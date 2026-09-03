# Sh'elah — Architectural & Implementation Decisions

Internal reference. Purpose: be able to defend every non-trivial choice in this codebase in an unscripted technical conversation. Not marketing copy — includes what's unfinished, what's inconsistent, and what would embarrass you if a reviewer pushed on it.

Compiled by deep-reading the actual current code (not memory, not docstrings alone) as of 2026-08-19. File:line references are current as of that read; re-verify before citing them if significant time has passed. **Exception:** the rate-limiting sections (§6 "Rate limiting," and the related passages in the Clerk-JWKS aside, the dual-transport section, the deployment/observability "Suspicious" lists, and Weakest-point #4) were explicitly refreshed 2026-08-26 to reflect the `plan.md` §16.9 rate-limiter unification and the operator-confirmed production `RATE_LIMIT_REDIS_URL` value — everything else in this file is still frozen at the original 2026-08-19 read.

---

# 1. Dual-LLM routing (Claude vs Gemini)

## Dual-provider architecture: Gemini primary, Claude fallback

- **What was chosen:** Every `/ask` synthesis call tries Google Gemini first; only on failure does it fall through to Anthropic Claude (`claude-haiku-4-5`). True in both the production async path and the dev/self-hosted sync path, implemented as two separate call chains.
- **Where it lives:**
  - Async (production): `backend/claude.py:1837-1896` `ask_ai_async()` calls `_call_gemini_httpx_model()` (line 1884), and only if `result_error` is set and doesn't start with `"security_blocked"` (line 1891), calls `_call_anthropic_httpx_model()` (line 1892).
  - Sync (non-production): `backend/claude.py:1421-1450` `_call_primary_model()` calls `_call_gemini_model()` (line 1422) then `_call_claude_model()` → `_call_anthropic_httpx_model()` (line 1446) on the same condition.
  - Model constants: `_DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"` (line 62); Claude fallback hardcoded as `"claude-haiku-4-5"` (line 1690) — deliberately a code-level literal, not env-configurable, since a model swap requires a redeploy on Vercel anyway.
- **Problem it solves:** Single-provider dependency means any outage, rate-limit event, or API deprecation from one vendor takes down the core product feature entirely. Two independent providers give a live fallback without local model hosting.
- **Alternatives that existed:** (1) Single provider with only the SDK's own retry/backoff — simpler, but a full outage or account-level rate-limit event fails all requests. (2) A model-routing gateway/proxy (LiteLLM, OpenRouter, Vercel AI Gateway) abstracting provider selection — would remove the hand-rolled duplication documented below, at the cost of another vendor in the chain. (3) Parallel dispatch to both providers, take whichever returns first — lower tail latency, but pays for both calls every request, conflicting directly with the per-request cost-ceiling design.
- **Why this won:** Cost. Gemini Flash-lite is roughly an order of magnitude cheaper per token than Claude Haiku by the app's own price table (`backend/cost_meter.py:33` `gemini-1.5-flash` $0.075/$0.30 per 1M tokens vs. `backend/cost_meter.py:27` `claude-haiku-4-5` $0.80/$4.00 per 1M — though see the pricing-table gap below, the *current* Gemini model name isn't actually in this table). Git history shows Gemini was added first as a secondary/fallback provider (`6853643`), then inverted to primary shortly after (`eaeaa4c`) — consistent with a cost-driven flip once Gemini's quality was judged sufficient, with Claude kept as the qualitative safety net.
- **What would break if removed:** Removing Gemini reverts to Claude-only at ~10x the per-call cost. Removing Claude removes the only fallback — a Gemini account suspension, quota exhaustion, or breaking SDK change takes `/ask` fully offline beyond the non-AI fallback ladder (`_run_ask_async_fallback`, `asgi.py:557-630`, which returns discovered Sefaria references without a synthesized ruling).
- **Follow-up questions I should be able to answer:**
  - Why Gemini and not OpenAI as the primary? (Not evidenced in code/commits — have the real answer ready.)
  - What happens if both providers fail on the same request? (`ask_ai_async` returns an error dict; `_run_ask_async_ai_synthesis` raises `RuntimeError`, caught in `ask_async`'s try/except, routed to the non-AI Sefaria-reference fallback.)
  - Is there a per-request cost from always trying Gemini first even when it's known to be down? Yes — see the circuit-breaker gap below.

## Fallback trigger logic: string-prefix matching on the error field, deliberately excluding safety referrals

- **What was chosen:** `.env.example` and `README.md` both describe "Gemini checked first, Anthropic fallback," and the code matches exactly. The trigger condition is precise: `result_error = str(result.get("error") or ""); if result_error and not result_error.startswith("security_blocked")`. Fallback fires only for genuine provider failures, never for a query intentionally routed to a safety referral or blocked by input validation — those bypass model calls entirely.
- **Where it lives:** `.env.example:8-9`, `README.md:143`, `backend/claude.py:1884-1896` (async), `:1422-1450` (sync). Safety-referral classes: `SAFETY_REFERRAL_CLASSES`, line 276.
- **Problem it solves:** Prevents a subtle bug class where a deliberate safety refusal ("this needs a referral, not a ruling") gets mistaken for a provider error and silently re-dispatched to Claude, which could then produce an actual halachic-sounding answer to a self-harm/abuse/medical query the pipeline is specifically designed to intercept before synthesis (`docs/AGE_AND_SAFETY_POLICY.md:85-87`).
- **Alternatives that existed:** A boolean `is_fallback`/`blocked` flag instead of string-prefix matching. String-prefix matching on an error message is inherently fragile — and this exact fragility already caused a real production bug once (`docs/AGE_AND_SAFETY_POLICY.md:138-140`).
- **Why this won:** The error-string convention predated the safety-referral work; reusing it avoided inventing a second gating field. `backend/claude.py:981-987`'s comment names the historical bug directly.
- **What would break if removed:** Every safety referral would be treated as a normal error and re-dispatched to Claude for actual synthesis — defeating the entire age/safety referral layer.
- **Follow-up questions I should be able to answer:**
  - What's the exact set of error strings that DO trigger fallback? Anything not prefixed `security_blocked`: `gemini_error:*`, `gemini_sdk_error:*`, `gemini_client_missing`, `gemini_config_error:*`, `gemini_sdk_missing`.
  - Is this contract tested? Yes — a dedicated regression test (`test_error_string_starts_with_security_blocked`) pinned to the exact historical failure (`docs/AGE_AND_SAFETY_POLICY.md:146`).

## Three-layer timeout/retry/budget system

- **What was chosen:** Three independent, nested mechanisms: (1) a per-call SDK/HTTP-client timeout (`MODEL_REQUEST_TIMEOUT_SECONDS = 50`, `backend/claude.py:92`); (2) a tenacity retry decorator on Gemini's `ResourceExhausted` (429) errors only, capped at 3 attempts with exponential backoff (`backend/claude.py:498-523`); (3) a total wall-clock budget wrapping the entire primary+fallback sequence (`AI_TOTAL_BUDGET_SECONDS`, default 45s, env-overridable, `backend/claude.py:97`).
- **Where it lives:** Per-call timeout passed to Anthropic client construction (`claude.py:390,410`) and to Gemini's `HttpOptions(timeout=MODEL_REQUEST_TIMEOUT_SECONDS * 1000)` (`:446`). Tenacity retry: `_generate_gemini_content_with_retry()` (`:498-523`), used only by sync `_call_gemini_model()` (`:565`). Total budget enforced at the call site: `asgi.py:448-462` wraps `claude.ask_ai_async(...)` in `asyncio.wait_for(timeout=claude.AI_TOTAL_BUDGET_SECONDS)`; `app.py:1761-1775` wraps the sync path with `_ask_future.result(timeout=claude.AI_TOTAL_BUDGET_SECONDS)`.
- **Problem it solves:** A hung/slow model call must not hang the whole `/ask` request indefinitely, especially since Vercel bills Provisioned Memory for the full wall-clock duration — latency reduction is cost reduction on this platform. The total budget also has to stay under `vercel.json`'s `maxDuration: 90` so the platform never hard-kills the request before the graceful fallback gets a chance to run.
- **Alternatives that existed:** A single global timeout with no per-provider retry (simpler, but a transient 429 immediately burns the more expensive fallback instead of self-healing). A shared, provider-agnostic retry wrapper used identically by both sync and async Gemini call functions — would have prevented the drift noted below.
- **Why this won:** Layered defense — total budget guarantees the platform-level SLA; per-call timeout is a secondary bound; retry smooths transient rate-limiting without paying the fallback cost.
- **What would break if removed:** Reintroduces the original bug this was built to fix — `MODEL_REQUEST_TIMEOUT_SECONDS` was, for months, defined via env lookup but never actually passed to any SDK client (`tests/test_config_lint.py:1-13` now fails CI if any such constant becomes referenced nowhere else).
- **Follow-up questions I should be able to answer / concrete gaps to own:**
  - **Do the three mechanisms actually compose correctly? No.** `MODEL_REQUEST_TIMEOUT_SECONDS` (50s) is numerically *larger* than `AI_TOTAL_BUDGET_SECONDS` (45s default). Since the total budget wraps the entire primary+fallback sequence, the outer timeout always fires first. Under current defaults, `MODEL_REQUEST_TIMEOUT_SECONDS` is dead as an enforcement mechanism.
  - **Does the tenacity retry actually run in production? No.** It's wired only into the sync `_call_gemini_model()`, which is unreachable through the production Vercel deployment. The async `_call_gemini_httpx_model()` that production actually calls has zero retry logic — any first-attempt exception (including a 429) falls straight to the Claude fallback.
  - **Can the total-budget `wait_for` actually preempt a stuck call?** For the async production path, yes (fully `await`-based, genuinely cancellable). For the sync escape-hatch bridge (`_call_primary_model_sync`, `:1491-1530`), the code's own comment admits the outer timeout is "a defensive backstop... not primary enforcement," because `Future.result(timeout=...)` can't force-cancel a blocking call already in flight in a worker thread.

## Sync (app.py/Flask) vs. async (asgi.py/FastAPI) `/ask` duplication

- **What was chosen:** Two parallel, independently-implemented `/ask` handlers: `app.py:1941` (Flask/WSGI, calls `claude.ask_claude`) and `asgi.py:643` (FastAPI/ASGI, calls `claude.ask_ai_async`).
- **Where it lives:** `asgi.py:730` mounts Flask at `fastapi_app.mount("/", WSGIMiddleware(flask_app_module.app))`, but the FastAPI `/ask` route is registered earlier (`:633-643`) — Starlette matches explicit routes before falling through to a mount, so the FastAPI `/ask` always wins for any request reaching this entrypoint.
- **Problem it solves / actually is:** Not simply leftover dead code — genuine, documented multi-deployment support. `api/index.py:14` (`from asgi import app`) is what Vercel actually serves, so in production the Flask sync `/ask` is unreachable. But `README.md:118-128` documents legitimate additional run modes: `uvicorn asgi:fastapi_app` (matches Vercel), `python3 app.py` (plain Flask, no async pipeline), and `README.md:211-215` a fourth: `gunicorn app:app` as a supported self-hosted WSGI production option — where the Flask sync `/ask` is the only `/ask` and fully live.
- **Alternatives that existed:** Delete the Flask `/ask` route entirely, only support ASGI deployment (breaks the documented WSGI self-host path). Make the Flask route a thin async-bridge shim calling the same code the FastAPI route calls — would eliminate drift risk at the cost of it no longer being a "real" synchronous implementation for gunicorn.
- **Why this won (or why the risk was accepted):** Supporting plain WSGI/gunicorn genuinely requires a synchronous path (WSGI has no native async). The two routes share the downstream `_call_anthropic_httpx_model`/`_call_claude_model` function — explicitly refactored to be shared after a prior incident where the duplicate lacked the `record_llm_call` cost-tracking call the async copy had, so token usage from that path silently never reached the cost meter (comment at `claude.py:1406-1418`). That exact class of drift was fixed for the Claude call, but not for the Gemini call.
- **What would break if removed / concrete drift already present:** The Gemini primary call is **not** shared — `_call_gemini_model()` (sync, blocking `genai.Client`, `:526-617`) and `_call_gemini_httpx_model()` (async, `.aio.models.generate_content`, `:1756-1834`) are two independently-written implementations that already differ: (1) sync has tenacity retry-on-429, async (production) has none; (2) sync's `is_simple_q` cutoff is `max_tokens <= 1024` (`:603`) while async's analogous logic is driven by a separately-computed `is_simple` boolean passed in from `ask_ai_async` (`:1787`) — same intent, different code, no shared source of truth. Nothing currently tests that the two stay behaviorally identical.
- **Follow-up questions I should be able to answer:**
  - If someone self-hosts via `gunicorn app:app`, do they get retry-on-429 for Gemini that Vercel users don't? Yes.
  - Why wasn't the Gemini call function unified the same way the Anthropic one was? Not evidenced in commit history — have a real answer ready.

## Age-appropriate output: a prompt-layer directive, not a model/provider selector

- **What was chosen:** Age-appropriateness is a single shared system-prompt directive (`AGE_APPROPRIATE_DIRECTIVE`, `backend/claude.py:1060-1066`) appended to both `CORE_SYSTEM_PROMPT` and `SIMPLE_SYSTEM_PROMPT`, applying identically regardless of which provider answers. Age/mode never changes which model or provider is called — only what every model is instructed to do.
- **Where it lives:** `backend/claude.py:1060-1111` (directive + appended to both prompts); pre-synthesis routing in `classify_safety()` (`:918-951`), which can bypass model synthesis entirely for `medical`/`mental_health_or_self_harm`/`abuse_or_minor_safety` classes; post-generation scan in `validate_model_output()` (`:1031-1057`). Documented in `docs/AGE_AND_SAFETY_POLICY.md:54-122`.
- **Problem it solves:** The app has no real age verification (`docs/AGE_AND_SAFETY_POLICY.md:42-44`: "No age or date of birth is collected") — so there's no signal available at request time to route a distinct "mode" anyway. The three-layer defense (prompt directive → pre-synthesis classification → post-generation scan) exists because none of the three alone is trustworthy.
- **Alternatives that existed:** A stricter prompt variant selected by a user-declared age field (requires collecting age, rejected on COPPA-avoidance grounds per `docs/AGE_AND_SAFETY_POLICY.md:12-18`). Routing sensitive classes to a different/cheaper/more-restrictive model instead of a static referral — rejected in favor of bypassing model synthesis entirely for the highest-severity classes (referral text is templated, not model-generated, for the three referral classes).
- **Why this won:** One directive applied everywhere avoids "wrong mode selected → wrong prompt used" bugs, and works uniformly regardless of which provider answers (provider choice is driven by availability, not by age/mode). `PROMPT_VERSION` (`:115`, `"2026-07-30-age-appropriate-v1"`) is bumped whenever the shared prompt constants change materially, so a stored answer's governing prompt version stays reconstructable.
- **What would break if removed:** Removing the directive from even one of the two system prompts would create a silent asymmetry where simple questions get unfiltered output.
- **Follow-up questions I should be able to answer:**
  - Does `mode` (balanced/practical/sources/strict) or `answer_language` ever change which provider is called? No — confirmed by reading both call paths; they only affect prompt content and referral-text language, never provider selection.
  - What happened to the original, broader `medical` classifier? It originally caught any personal-medical + halachic-keyword combination and over-refused the most common real question class (fasting + pregnancy/diabetes); redesigned to require first-person, present-tense, acute-emergency framing only, after a documented adversarial review that found 17 bugs before shipping (`docs/AGE_AND_SAFETY_POLICY.md:132-152`).

### Suspicious / thin areas — dual-LLM routing

- **Cost-meter price table doesn't include the actual production Gemini model.** `backend/cost_meter.py:33-35` only has `gemini-1.5-flash`, `gemini-1.5-pro`, `gemini-2.0-flash`. Production actually uses `_DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"` (`claude.py:62`, overridable via `GEMINI_MODEL`). `estimate_cost_usd()` falls back to `$0.00`/`$0.00` for any unmapped model (`cost_meter.py:41,44-47`). **The majority of real production LLM calls are being logged at $0.00 cost, silently** — this directly undermines both the per-user daily budget ceiling and the global daily-spend alert, since both read from the same `ai_usage_log.cost_usd` column.
- **Gemini retry-on-429 is dead in production**, as detailed above — only reachable via the unreachable-in-production sync path.
- **The circuit breaker names Gemini/Claude as tracked services but is never wired into the actual call path.** `backend/health_check.py`'s `_circuits` dict tracks Sefaria/Hebcal/Gemini/Claude; `is_healthy()` is called from calendar/search-provider code, but never once from `backend/claude.py`, `app.py`, or `asgi.py` before dispatching to Gemini or Claude. `plan.md:829` claims circuit breakers skip known-dead AI providers "already a cost win" — this is not implemented. Every `/ask` request always attempts Gemini fresh, even immediately after consecutive failures.
- **`MODEL_REQUEST_TIMEOUT_SECONDS` (50s) vs. `AI_TOTAL_BUDGET_SECONDS` (45s default) are not numerically coordinated** — the outer budget always wins first, as detailed above. Not broken, but clearly undertuned, and worth being ready to explain rather than caught flat-footed.
- **`check_daily_budget_and_alert()`'s own docstring admits it's an after-the-fact alert, not an enforcement ceiling** (`cost_meter.py:181-186`) — and given the pricing-table gap above, neither mechanism can currently be trusted to reflect real Gemini spend regardless of how well the mechanism itself is designed.
- **Dangling `plan.md` anchor cited in load-bearing comments** (`claude.py:94` cites "§7.14", `tests/test_ask.py:429` cites "§7.13" — `plan.md` itself says the old numbered-subsection scheme is dead and these should point to §19). Minor, but exactly the kind of self-contradicting citation a sharp reviewer will pull on.

---

# 2. Halachic source retrieval pipeline

## No embeddings — retrieval is keyword/dictionary matching, plus whatever ranking Sefaria's own search API does server-side

- **What was chosen:** No vector index, no embedding model, no similarity search anywhere in the codebase (confirmed via grep across `requirements.txt`, `backend/`, `app.py`, `asgi.py`). Two entirely separate keyword mechanisms serve the live `/ask` route:
  1. **Primary path** — `backend/sefaria.py`'s `TOPIC_REFS` dict (`:27-182`, ~150 hand-curated `"keyword": [Sefaria refs]` entries) matched by `find_refs_for_question()` (`:253-272`) via plain `if keyword in q_lower` substring checks — no stemming, no weighting, first 7 matches win, hardcoded 2-ref fallback if nothing matches.
  2. **Fallback path** (`backend/utils/search_provider.py::get_halakhic_sources`, line 719) — regex-tokenizes the query, POSTs to Sefaria's own `search-wrapper` endpoint with `"field": "naive_lemmatizer"`, `"sort_method": "score"`, `"sort_fields": ["pagesheetrank"]` — real ranking happens server-side inside Sefaria's own infrastructure, not this app's. Local hits are then *filtered* (not ranked) by `_is_sefaria_hit_relevant()` — a substring-in-haystack check against the same regex keywords.
  3. Community-customs relevance (`backend/rag.py::_score_community_knowledge_row`, line 118) is additive point-scoring: +8 topic hit, +4 source hit, +2 content hit, +8 community-lens match — a hand-tuned TF-style scorer.
  4. Library title search (`backend/sefaria_library.py::_score_catalog_row`, line 810) is a separate hand-tuned scorer: base 60, exact title match 120, prefix match 100, substring 85, plus two hardcoded special-case boosts.
- **Where it lives:** `backend/sefaria.py:27-272`, `backend/utils/search_provider.py:203-840`, `backend/rag.py:102-256`, `backend/sefaria_library.py:810-898`.
- **Problem it solves:** Ships a working retrieval system without standing up an embedding pipeline, vector DB, or per-query embedding cost. For a domain with a bounded, well-known vocabulary (halachic terms, book titles, holiday names), curated keyword→ref maps have high precision for the head of the query distribution.
- **Alternatives that existed:** (a) Embed the Sefaria corpus (or at minimum the ~150 topics) once and do cosine-similarity retrieval — generalizes to paraphrased/novel phrasing the keyword dict misses entirely; (b) use any semantic-adjacent "related topics" API Sefaria might expose; (c) hybrid — keyword pre-filter then embedding rerank on the candidate set.
- **Why this won:** Zero infra cost, zero added latency beyond a dict lookup, full transparency/debuggability (read `TOPIC_REFS`, know exactly why a ref was or wasn't returned). Tradeoff: recall is capped at what someone thought to hand-type into the dict — synonyms, mixed Hebrew/English phrasing (`QUERY_BROADENER_MAP` has only 9 entries), or paraphrased questions fall straight through to the "internal-ai-needed" tier with no primary/customs source backing.
- **What would break if removed:** Every query not literally containing one of ~150 curated keywords returns zero primary sources and falls to the internal-AI-knowledge tier — answering from the model's training data with a disclaimer instead of a cited source. A real correctness/trust regression for an app whose value proposition is sourced answers.
- **Follow-up questions I should be able to answer:**
  - What fraction of real queries hit `TOPIC_REFS` vs. the fallback ladder vs. "internal-ai-needed"? No telemetry currently captures this split.
  - Why does `_is_sefaria_hit_relevant` re-derive keywords locally instead of trusting Sefaria's own relevance score (captured but never used to sort/filter)?
  - Has `TOPIC_REFS`'s precision/recall ever been measured against a labeled query set, or is coverage judged by feel?

## Sefaria caching — three-tier (memory → disk → network), circuit breaker covers only half the call sites

- **What was chosen:** `backend/sefaria_library.py::_cached_get()` (`:298-353`) checks, in order: (1) in-process `TTLCache` (`maxsize=2048, ttl=3600`); (2) disk cache under `.sefaria_cache/` keyed by MD5(url), 7-day TTL; (3) live network via a shared session with a browser-spoofed User-Agent. On a 403, `_sefaria_block_status` tracks consecutive blocks for a diagnostics endpoint — passive bookkeeping, not request-blocking. A real circuit breaker exists (`backend/health_check.py`, `APIHealth`, 3-failure threshold, 120s recovery) but is wired only into `search_provider.py`'s fallback-ladder calls, never into the primary `sefaria.py`/`sefaria_library.py` path hit on every `/ask` request.
- **Where it lives:** `backend/sefaria_library.py:34-140,298-353`, `backend/cache.py` (generic TTLCache), `backend/health_check.py`.
- **Problem it solves:** Sefaria's public API rate-limits/Cloudflare-blocks aggressive callers; disk cache aims to survive process restarts; memory cache avoids re-hitting disk within one process.
- **Alternatives that existed:** A real local mirror/snapshot of the corpus (avoids the live dependency, large storage commitment); Redis/Upstash for the disk-cache tier (survives across instances, not just one — relevant since the on-disk tier's actual persistence guarantee on Vercel is unverified in this pass); a circuit breaker wired uniformly into every Sefaria call site.
- **Why this won:** Cheap to build, no extra infra. Tradeoff: unverified whether the disk cache actually persists on Vercel's serverless filesystem model (see caching section for the concrete finding that it likely does not).
- **What would break if removed:** Every request re-hits Sefaria live, multiplying latency (12s timeout per network call) and 403-block risk. Without the (partial) circuit breaker, the fallback ladder hammers a downed endpoint on every request until timeout instead of failing fast.
- **Follow-up questions I should be able to answer:**
  - Why is the circuit breaker wired into the fallback ladder but not the primary path hit on every single `/ask` request?
  - What's the actual cache hit rate in production, given serverless cold starts likely reset the in-memory tier every few requests?

## Customs corpus — hand-authored JSON per community, migrated into Supabase; the live `/ask` path queries Supabase, not the local JSON

- **What was chosen:** 15 JSON files under `customs/` (13 structured community files + 1 legacy + 1 schema), hand-authored with `heritage_id`, `core_halachic_authorities`, `halacha_index`, `unique_minhagim`, `source_registry`. `scripts/migrate_customs_to_supabase.py` upserts these into `community_knowledge` (deterministic IDs via sha256). **The live `/ask` route never touches the local JSON at request time** — `backend/rag.py::_retrieve_community_knowledge()` (`:214`) queries Supabase directly via `ilike`. `backend/customs.py`'s local-JSON + `difflib` fuzzy-match implementation is a separate, parallel implementation **not reachable from any live route** — its only non-test caller (`data_service.py::get_customs()`) is itself called from nowhere except a test. Its one genuinely live entry point is `validate_all_customs_at_startup()`, JSON-schema validation at boot, not retrieval.
- **Where it lives:** Source: `customs/*.json`. Migration: `scripts/migrate_customs_to_supabase.py`. Live retrieval: `backend/rag.py:118-256`. Dead parallel path: `backend/customs.py:172-255` + `backend/data_service.py:45-47`.
- **Problem it solves:** Sefaria's corpus is normative/canonical; it doesn't encode community-specific practice variation (e.g. kitniyot definitions for Ashkenazim vs. Sefardim). An LLM asked cold would hallucinate or flatten distinct traditions. A hand-maintained, source-attributed dataset per community gives ground truth the LLM's own knowledge can't be trusted for.
- **Alternatives that existed:** Ask the LLM to answer community-specific questions from its own training knowledge with a stronger prompt instruction (cheaper, unverifiable/unsourced); query Sefaria for community-tagged content (largely doesn't exist at the source); keep the JSON as sole runtime source, skip the Supabase migration (loses queryability).
- **Why this won:** A small, hand-vetted corpus is checkable by a human for halachic accuracy in a way an LLM's internal knowledge isn't; per-community structure lets the app apply a "community lens" filter that a flat prompt can't reliably self-apply. Tradeoff: now two representations of the same data (JSON source + Supabase runtime copy) that must be kept in sync by manually rerunning the migration — no evidence of automated sync.
- **What would break if removed:** Every community-specific question loses ground-truth source and the LLM prompt drops to Sefaria-only/internal-knowledge tiers — community practice differences (a core differentiator, per the 14-heritage-group registry in `backend/helpers.py:173-188`) stop being answerable with attribution.
- **Follow-up questions I should be able to answer:**
  - Is `backend/customs.py`'s dead local-JSON path intentionally kept as a fallback-in-waiting, or just stale? Nothing in the code states intent either way.
  - What happens when `customs/*.json` is edited but the migration isn't rerun? The live app silently serves stale community data indefinitely — nothing invalidates the Supabase copy against the JSON.

## `backend/ask_pipeline.py` — confirmed dead code; both live `/ask` implementations independently duplicate its logic instead of using it

- **What was chosen (or rather, what actually happened):** `backend/ask_pipeline.py` (515 lines) is a from-scratch async reimplementation of the entire `/ask` orchestration — prayer short-circuit, parallel source/knowledge collection, strict-mode guard, AI synthesis, fallback ladder — intended per its own docstring to become the single source of truth shared by both transports. **It is not imported or called anywhere in production.** Its own docstring: *"NOT imported or called by app.py or asgi.py... Do not treat this module as drop-in-safe."* Meanwhile `app.py:1941` (sync) and `asgi.py:634` (async) each have their own complete, independently-implemented, structurally near-identical pipelines. **There are, right now, three implementations of the same `/ask` orchestration** — two live and diverging independently, one dead and explicitly unverified against the current shape of the rest of the codebase.
- **Where it lives:** Dead: `backend/ask_pipeline.py` (whole file). Live #1 (sync): `app.py:1490-1975`. Live #2 (async): `asgi.py:141-680`.
- **Problem it solves (intended, not actual):** If wired in, would eliminate the exact duplication that exists today between the Flask and FastAPI handlers.
- **Alternatives that existed:** Finish the migration — cut both live handlers over and delete the duplicated logic (the stated original intent); never build a shared module, accept duplication as the cost of two transports; delete `ask_pipeline.py` now rather than leaving 515 lines of unverified, 0%-production-exercised code in the tree.
- **Why this won (i.e., why it's in this half-finished state):** Has the signature of a real migration that got scoped, partially built (compiles, has a full test suite for its pure helpers), and stalled — likely because cutting over two live, business-critical routes to a new shared implementation is riskier than writing the module was, and it never got prioritized to finish. Classic partial-migration risk: it *looks* production-ready (professional docstrings, structurally sound, passing tests) but only its own docstring prevents someone assuming it's live.
- **What would break if removed:** Nothing — that's the point. Deleting the module and its two dedicated test files would have zero effect on the running app. The only "break" is losing the smoke test that exists purely to catch `app.py` attribute drift out from under the dead module.
- **Follow-up questions I should be able to answer:**
  - Given the two live pipelines have already diverged in small ways since this was written, is it even salvageable, or does finishing the migration now mean rewriting it against the current two implementations rather than merging it in?
  - Why keep 515 lines + 2 test files of confirmed-dead code in the tree instead of finishing the cutover or deleting it?
  - Does `tests/test_ask_pipeline.py` exercise `run_ask_pipeline()`'s real logic, or just check that `app.py` still has the attributes the dead module expects? (Per its own docstring — the latter.)

## Two independent retrieval code paths: the AI `/ask` pipeline and the human-browsing library reader don't share ranking/query logic

- **What was chosen:** The AI answer pipeline resolves sources via `sefaria.py`'s curated `TOPIC_REFS` dict plus `search_provider.py`'s keyword-driven discovery ladder. The human-facing library/reader UI (`backend/routes_library.py`) resolves sources via `sefaria_library.py`'s own `search_library()`/`_search_index_catalog()` — a completely different title-match scoring function with its own cache tiers. Both ultimately call `sefaria_library.py::get_text()` to fetch content once a ref is resolved, so text-fetching and caching are shared — but *finding* the ref is fully duplicated with different algorithms and different keyword-extraction functions.
- **Where it lives:** AI path: `backend/sefaria.py` + `backend/utils/search_provider.py`. Human path: `backend/routes_library.py` (pulls from `backend.sefaria_library`, never from `backend.sefaria` or `backend.utils.search_provider`).
- **Problem it solves:** The two use cases genuinely differ — the AI pipeline needs the best few refs for a natural-language question with graceful multi-tier fallback; the library reader needs exact-title/browse-style search (a user typing "Rambam" expects title-prefix matches, not halachic-topic inference).
- **Alternatives that existed:** A single shared retrieval module with a mode flag; or at minimum sharing the keyword-extraction/tokenization utility (currently two different regexes doing conceptually the same job).
- **Why this won:** Organic growth — the two features shipped at different times and nobody has unified them since. Reasonable given the different UX requirements, but means bug fixes to relevance/keyword handling in one place don't propagate to the other.
- **Follow-up questions I should be able to answer:**
  - Has the Hebrew-prefix-stripping/keyword-normalization logic ever drifted between the two in a way that caused a real bug?
  - Is there a plan to unify these, or is the split considered final?

## Retrieved context is wrapped in an explicit `<retrieved_context>` prompt-injection boundary

- **What was chosen:** Every piece of retrieved data injected into the system prompt — Supabase community knowledge, stored user-memory summaries, tool context — is wrapped via `_wrap_retrieved_context()` (`backend/claude.py:1360-1374`) as `<retrieved_context source="...">...</retrieved_context>`, with an explicit prompt instruction that content inside these tags is data, never instructions, even if it reads as imperative.
- **Where it lives:** `backend/claude.py:1360-1398`, boundary instruction at `:1093`.
- **Problem it solves:** Without this, retrieved text could be crafted or coincidentally phrased to look like an instruction to the model — a classic RAG prompt-injection vector.
- **Alternatives that existed:** Do nothing, trust curated/internal-table content is benign; a stricter structural separation via provider tool-use/function-result primitives instead of string-concatenated tags.
- **Why this won:** Cheap to implement, and honestly framed in the code's own comments as hardening *before* a future feature needs write access — `community_knowledge` is read-only from every live route today, `user_memories` entries come only from the model's own moderated output.
- **What would break if removed:** No known live exploit breaks today, but if a future feature (user-submitted notes, unmoderated community contributions) added a write path into these tables, unmarked content would flow into the system prompt, restoring a real injection vector.
- **Follow-up questions I should be able to answer:**
  - Is there any current or planned feature letting end users write into `community_knowledge`/`user_memories` directly?
  - Has this convention ever been red-teamed against either live model?

### Suspicious / thin areas — retrieval pipeline

- **`backend/ask_pipeline.py` is fully dead but still in the tree with two dedicated test files**, and reads as production-ready to anyone who doesn't read its docstring carefully. The single biggest "looks more finished than it is" risk in the pipeline.
- **`backend/customs.py`'s local-JSON fuzzy-match path is also effectively dead** — a second, quieter orphan of the same shape, undocumented as such (unlike `ask_pipeline.py`, nothing flags it).
- **No embeddings/vector search anywhere, dressed up with real-sounding function names** (`_score_community_knowledge_row`, `_is_sefaria_hit_relevant`). It's hand-tuned point scoring and hardcoded topic dictionaries, not machine-learned relevance. Not necessarily wrong for the corpus size and domain, but if this is ever described externally as "semantic search" or "AI-powered retrieval," that would be inaccurate.
- **The circuit breaker only covers half the Sefaria call sites** — absent from the primary path hit on every `/ask` request and every library-reader page view.
- **Two live, independently-maintained `/ask` implementations that already risk drifting**, with the shared-implementation module that would have prevented this abandoned mid-migration.
- **The customs-corpus JSON→Supabase migration has no visible sync/invalidation mechanism.**
- **`.sefaria_cache/`'s actual persistence guarantee in production is unverified** — see the caching section for the concrete finding that it's very likely a no-op on Vercel.
- **`get_halakhic_sources`'s final tier answers from the model's own internal knowledge with only a disclaimer, no source at all.** For a halacha Q&A app whose core value proposition is sourced answers, this is the weakest link in the pipeline, and there's no visibility into how often it's actually hit in production.

---

# 3. Data model / schema decisions (Supabase)

## Why Postgres/Supabase at all (vs. Firebase, a KV store, SQLite)

- **What was chosen:** Supabase-hosted Postgres, accessed almost exclusively through `supabase-py` (`requirements.txt:35`, `supabase==2.28.3`), via the backend's own service-role key as the default access path.
- **Where it lives:** `app.py:925-935` (`_get_supabase_client()`, service-role singleton); every write/read path in `backend/routes_user.py`, `backend/rag.py`, `backend/cost_meter.py`, `backend/routes_privacy.py`.
- **Problem it solves:** Vercel serverless functions have no persistent local disk between invocations (rules out SQLite). The app also needs `jsonb` columns for variable-shaped AI output, trigram fuzzy text search over `community_knowledge` via `pg_trgm` GIN indexes, and declarative row-level authorization (RLS) as a second layer beneath the app's own Clerk-JWT checks.
- **Alternatives that existed:** (1) Firebase/Firestore + Firebase Auth — rejected implicitly, the project deliberately keeps Clerk as sole identity and Supabase as pure data+RLS. (2) Upstash Redis/Vercel KV for simple prefs/bookmarks — breaks down for `ask_history`'s date-range queries and `ai_usage_log`'s `SUM(cost_usd)` budget aggregation. (3) SQLite via Turso/LiteFS or a mounted volume — not viable on stock Vercel serverless without extra infra.
- **Why this won:** One managed Postgres instance gives relational filtering, full-text-ish search, and RLS in a single HTTP-reachable system, without owning a second search/index service.
- **What would break if removed:** The budget-enforcement queries (`check_user_budget_and_enforce`, `check_daily_budget_and_alert`) depend on `WHERE created_at >= ?` range queries with pagination — a plain KV store has no equivalent without hand-maintained daily-bucket keys. `community_knowledge` retrieval depends on `ilike`/`or_` compound filters a KV store can't express.
- **Follow-up questions I should be able to answer:**
  - What Postgres-specific features are actually load-bearing? `pg_trgm` GIN indexes, `jsonb`/`text[]` typed columns, RLS.
  - Why not put `community_knowledge` in a vector DB, given this is RAG? Because retrieval is keyword/trigram-based — there is no vector column or `pgvector` extension anywhere.
  - What happens to `ai_usage_log` budget queries once row counts grow past a single day's pagination cap? Already handled with a 200-page safety backstop, logged as a warning if hit.

## RLS design keyed on Clerk `sub`, but only partially enforced

- **What was chosen:** RLS on `user_preferences`, `study_bookmarks`, `user_memories` compares `auth.uid()::text = user_id`, while `ask_history`'s RLS instead compares `user_id = (current_setting('request.jwt.claims', true)::jsonb ->> 'sub')` — a *different* mechanism from the other three tables. `ai_usage_log` has **no RLS at all** (confirmed via grep across every SQL file).
- **Where it lives:** `scripts/sql/SUPABASE_RLS_POLICIES.sql`, duplicated in `scripts/sql/bookmarks_and_preferences_setup.sql`; `scripts/migrate_ask_history.sql:34-36`. App-side, the only routes building an RLS-scoped client (`_get_user_scoped_supabase_client()`, `app.py:1061-1070`) are `/api/user/preferences` and `/api/bookmarks/semantic`. Every other Supabase-backed route — ask-history, all of `routes_privacy.py`, `cost_meter.py`, `community_knowledge` retrieval — uses the service-role client, which bypasses RLS entirely (`routes_privacy.py:9-20` says this out loud).
- **Problem it solves:** Defense-in-depth — if an app-layer `.eq("user_id", ...)` filter is ever forgotten in a new route, RLS is meant to be the backstop.
- **Alternatives that existed:** RLS-only enforcement everywhere (rejected — the frontend sends a plain default Clerk session JWT, not a Supabase-template one, and `routes_privacy.py:13-16` explicitly says gating DSR endpoints behind the RLS-scoped client "would 403 for exactly the users this feature exists to serve"). Service-role-only everywhere, no RLS at all (also not fully adopted — RLS is kept as a secondary gate for the handful of routes using the user-scoped client).
- **Why this won:** A hybrid where app-layer Clerk JWT verification is the actual authorization gate for essentially every route, and RLS is inconsistently layered on top for 3 of 5 user-scoped tables using two different SQL idioms — effectively dead code in practice for `ask_history`, since it's never queried through a user-scoped client.
- **Known/likely gap (not verifiable from the repo alone):** `auth.uid()` only resolves for a Clerk JWT if Supabase's project is configured to trust Clerk as a JWT issuer — a **dashboard setting, not captured anywhere in this repo**. There are two inconsistent client-side patterns for this: the live path forwards the raw plain bearer token, no template; a separate, non-imported debug utility (`scripts/clerk_supabase_rls.py`) expects a Clerk JWT minted from a named `"supabase"` template. If the dashboard-side trust isn't configured to accept the default token, `auth.uid()` returns NULL for every request, and the two RLS-scoped routes would silently return zero rows for every real user.
- **What would break if removed:** For the majority of routes (service-role path), nothing observable — RLS was never the enforcement mechanism there. For the two RLS-scoped routes, removing RLS wouldn't newly break anything either if it's not currently functioning as intended.
- **Follow-up questions I should be able to answer:**
  - Is Supabase Third-Party Auth (Clerk) actually enabled on the project, and where is that captured/versioned? It isn't — a single point of failure for the only two routes relying on RLS being real.
  - Why does `ask_history`'s RLS policy use a different idiom than the other three tables? No comment explains it; the policy has never been exercised.
  - Why does `ai_usage_log` (holding raw IP-derived `client_key` and per-user cost data) have zero RLS? Not addressed anywhere in the codebase.

## `user_preferences.user_id` type migration (UUID → TEXT)

- **What was chosen:** `ALTER TABLE public.user_preferences ALTER COLUMN user_id TYPE text USING user_id::text;` — explicitly idempotent.
- **Where it lives:** `scripts/migrate_user_preferences_user_id_to_text.sql`; consumed by `backend/routes_user.py`'s preferences get/put handlers.
- **Problem it solves:** If `user_id` was ever created (or dashboard-edited) as `uuid`, every call querying it with a Clerk ID like `user_2abcXYZ...` fails with Postgres error `22P02` — Clerk's user-id format is not a UUID. This is a real Clerk-vs-Supabase identity-format mismatch: Supabase's own `auth.users.id` is conventionally a UUID, which makes "user id column should be UUID" a natural but wrong assumption when the actual tenant key is a Clerk `sub` string.
- **Alternatives that existed:** Keep `user_id` as `uuid`, maintain a separate `clerk_id → internal_uuid` mapping table (more "correct" relationally, adds a join and sync step). Store the Clerk ID as `text` from day one everywhere (already true for `study_bookmarks`/`ask_history`/`ai_usage_log` — this migration exists precisely because `user_preferences` drifted from that, likely via manual dashboard table creation predating the setup SQL).
- **Why this won:** Matching every other Clerk-keyed table's `text` type is simpler than a UUID mapping layer, since Clerk (not Supabase Auth) is the actual identity system of record.
- **What would break if removed:** Every `GET`/`PUT /api/user/preferences` call would error with a Postgres type-cast error the moment the column is `uuid` and a Clerk string literal is passed — not hypothetical, it's the exact documented failure mode.
- **Follow-up questions I should be able to answer:**
  - Why would a column meant to hold Clerk IDs ever have been `uuid`? Almost certainly Supabase's own `auth.users.id` convention, applied by habit/dashboard default.
  - Is this migration confirmed applied to production? Unverifiable from the repo — see the "untracked files" gap below.

## Migration strategy: hand-run `.sql` files, no framework

- **What was chosen:** Every schema change is a standalone `.sql` file meant to be pasted into the Supabase SQL editor by hand — no Alembic, no `supabase/migrations/` directory, no `supabase db push` workflow (despite `docs/DATABASE.md:122-127` *claiming* this tooling is used — there is no `supabase/` directory anywhere in the repo).
- **Where it lives:** `scripts/sql/*.sql` (initial setup) + `scripts/migrate_*.sql` (incremental ALTERs) — nine files, no numbering/ordering convention, no migration-tracking table, no checksums.
- **Problem it solves:** Fast, zero-tooling-overhead schema changes for a solo-developer project.
- **Alternatives that existed:** Supabase CLI migrations (versioned files, tracked via `supabase_migrations.schema_migrations`, diffable against a shadow DB) — the industry-standard approach for exactly this stack. Alembic — heavier, less idiomatic without a SQLAlchemy ORM layer.
- **Why this won:** Speed of iteration for one developer, at the direct cost of any automated tracking of what's been applied where.
- **What would break if removed:** Nothing "breaks" — this is the absence of a process; the cost shows up as drift risk, not a runtime failure.
- **Follow-up questions I should be able to answer:**
  - Is there a single source of truth for "what does the schema look like right now"? No — `docs/DATABASE.md` claims to be that source and is substantively wrong (see below); the real source of truth is "whatever's been manually run against the live project," unobservable from the repo.
  - How would a new environment get an identical schema? Manually, running all nine files in an inferred order.
  - Is there any rollback path? No `DOWN`/reversal SQL exists anywhere.

## Table-by-table reality check

| Table | Created by | Queried in | Notes |
|---|---|---|---|
| `user_preferences` | `bookmarks_and_preferences_setup.sql` | `app.py:93`, `routes_user.py:235` | PK `user_id` (text) — NOT `clerk_id` as `docs/DATABASE.md:17` claims |
| `study_bookmarks` | `bookmarks_and_preferences_setup.sql` | `routes_user.py:307` | `docs/DATABASE.md:54` documents a differently-named/shaped `bookmarks` table that doesn't exist |
| `user_memories` | `rag_identity_cache_setup.sql` | `rag.py:308,405` | File promises "rag_identity_cache"; actual table is `user_memories` — `docs/DATABASE.md:36-50` documents a `rag_identity_cache` table that **does not exist anywhere** |
| `community_knowledge` | `rag_identity_cache_setup.sql` | `rag.py:216,229` | Public-read RLS, seeded by `scripts/migrate_customs_to_supabase.py` |
| `ask_history` | `migrate_ask_history.sql` | `routes_user.py:359,387`, `rag.py:393` | Only table with the `current_setting`-style RLS; never queried through an RLS client |
| `ai_usage_log` | `migrate_ai_usage_log_add_user_columns.sql` (its base `CREATE TABLE` is **not in this repo at all**) | `cost_meter.py:57,148,268` | No RLS anywhere |
| `queries` | documented in `docs/DATABASE.md:79-97` | **nowhere** | Does not exist in any SQL file or Python code — pure doc fiction |

### Suspicious / thin areas — Supabase schema

- **`docs/DATABASE.md` is substantially wrong**, and it's the one document meant to be the schema reference. It claims the tenant key is `clerk_id` (real: `user_id` everywhere), documents a `bookmarks` table (real: `study_bookmarks`), a `rag_identity_cache` table with columns that exist nowhere (the file it links to actually creates `community_knowledge` + `user_memories`), and a `queries` analytics table that doesn't exist at all. **This already caused a real bug**: `backend/routes_user.py:76-82` documents in-code that a previous version of `accept_legal()` used `on_conflict="clerk_id"` matching the wrong doc — PostgREST silently rejected every call, and the failure was swallowed by a broad `except`, so consent records were never actually persisted until caught.
- **Five schema-defining SQL files are untracked in git** (`migrate_ai_usage_log_add_user_columns.sql`, `migrate_ask_history_safety_metadata.sql`, `migrate_user_preferences_legal_consent.sql`, `migrate_user_preferences_legal_version.sql`, `migrate_user_preferences_user_id_to_text.sql`). These back real, currently-used columns (`safety_class`/`prompt_version` written on every completed answer; the per-user budget columns `check_user_budget_and_enforce()` needs). If this repo were re-cloned today, these files — and any record they were ever written — wouldn't exist, and there's no way from git history alone to know whether/when they were applied to production.
- **No RLS on `ai_usage_log`**, which holds raw `client_key` (IP strings) and per-caller cost data. Currently safe only because every access path uses the service-role client; would be exposed the moment the publishable/anon key is ever pointed at it directly.
- **Inconsistent RLS idiom between tables**, with the `ask_history` policy essentially untested in practice since it's never exercised via a user-scoped client.
- **Duplicate, byte-for-byte-identical RLS policy definitions across two files**, nothing enforcing they stay in sync if one is edited.
- **Whether RLS is even functionally alive depends on undocumented, unversioned Supabase dashboard configuration** — see the auth section for the fuller exploitability discussion.
- **No rollback scripts anywhere**, and **no CI check that any of this SQL is valid or was applied** — a broken/unapplied migration is discovered only at runtime, via broad `except`-and-log-nothing handlers that fail silently rather than surfacing schema problems.
- **No foreign keys anywhere** (expected, since Clerk not Postgres owns identity) — but it means zero referential integrity; an orphaned row for a deleted Clerk user is only cleaned up if `delete_account()` is actually invoked and succeeds on every table.

---

# 4. Auth and access control

## Clerk instead of custom auth or Supabase native auth

- **What was chosen:** Clerk as the sole identity provider. The app never stores passwords or handles credential verification; it only verifies Clerk-issued RS256 JWTs.
- **Where it lives:** `backend/auth.py` (verification), `templates/index.html:4095-4134` (frontend SDK, `authHeaders()`), `.env.example:13-19`.
- **Problem it solves:** Password storage, reset flows, session fixation, MFA, account-recovery UX are all delegated to a vendor instead of hand-rolled by a solo developer (`docs/PRIVACY_OPERATIONS.md:8-10`).
- **Alternatives that existed:** (1) Supabase's own `auth.users` + GoTrue — would unify identity and RLS under one vendor, `auth.uid()` would work natively with zero bridging code. (2) Custom email/password (Flask-Login/Argon2). (3) Auth0/NextAuth-style session cookies.
- **Why this won:** Clerk was picked for its frontend components independent of the data-layer choice; Supabase remains the data store. A deliberate "identity and data are different concerns" split — but this exact split produces the biggest problem in the codebase (see RLS below): Supabase's `auth.uid()` is designed around Supabase's own JWTs, not Clerk's.
- **What would break if removed:** Every route using the Clerk decorators loses its identity source entirely; `sub`-keyed tables would have no way to resolve "who is asking."
- **Follow-up questions I should be able to answer:**
  - Why not use Supabase Auth and skip the Clerk/Supabase identity bridge entirely?
  - What does Clerk's frontend session token actually contain, and where is that documented? Not documented — the app only reads `sub` and `sid`.

## JWT verification mechanics (JWKS fetch, caching, algorithm, issuer)

- **What was chosen:** `jwt.PyJWKClient` fetches Clerk's `.well-known/jwks.json`, resolves the signing key by `kid`, and `jwt.decode()` verifies signature (RS256), `iss`, expiry, and optionally `aud`.
- **Where it lives:** `backend/auth.py:32-67`. JWKS URL: `f"{issuer}/.well-known/jwks.json"` (`:40`).
- **Problem it solves:** Verifies a token was actually signed by Clerk for this issuer without the app holding Clerk's private key or calling Clerk synchronously on every request.
- **Alternatives that existed:** Call Clerk's session-verify Backend API on every request (network round-trip, worse latency/availability coupling). Hardcode/manually rotate the public key instead of JWKS.
- **Why this won:** JWKS + local signature verification is standard OIDC — no network call needed on the hot path once keys are cached.
- **Caching specifics** (verified against installed `PyJWT==2.13.0` source): `PyJWKClient(jwks_url)` uses all defaults — `cache_jwk_set=True`, `lifespan=300` (5-minute TTL on the entire JWK Set), `cache_keys=False` (per-`kid` cache not enabled, redundant given Tier-1 caching). The client itself is memoized module-level, so cache lifetime is bounded by process lifetime, not request lifetime.
- **What happens if Clerk's JWKS endpoint is unreachable:** `fetch_data()` raises a connection error on a cache miss; not treated specially anywhere — propagates up and is caught by a generic `except Exception`, returning **401 "Invalid or expired Clerk token"**. Indistinguishable from a genuinely bad token — a Clerk outage looks like "your session expired," not "the server is down." On Vercel, whether this bites depends on whether the invoking container is warm (JWKS cached) or cold — the same category of per-instance-state fragility the rate limiter used to have before it moved to a Redis-backed shared store (`plan.md` §16.9, 2026-08-22; see the rate-limiting section below). The JWKS cache itself remains process-local today — never called out or fixed for that specifically.
- **What would break if removed:** No signature verification at all — anyone could forge a JWT with any `sub` and be treated as that user.
- **Follow-up questions I should be able to answer:**
  - What's the blast radius of a Clerk JWKS outage? Fails closed (401 for anyone without a cached key), which conflates outage with bad credentials.
  - Why is it fine to leave `cache_keys=False`? Tier-1 JWK-Set cache already avoids the network call for 5 minutes; Tier-2 would only save re-scanning an already-in-memory list.

## `CLERK_ENFORCE_AUTH` — opt-in-per-route enforcement gate

- **What was chosen:** Two decorators with different semantics: `require_clerk_auth` always demands a valid token (401 if missing/invalid); `maybe_require_clerk_auth` only demands one if `CLERK_ENFORCE_AUTH` is true, otherwise runs unauthenticated but still validates any token present. `CLERK_ENFORCE_AUTH` defaults to `true` when `VERCEL == "1"` or `FLASK_ENV == "production"`, else `false`.
- **Where it lives:** `backend/auth.py:70-103`; consumed by both `/ask` implementations, `accept_legal`, `segment-report`, `export_chapter`.
- **Problem it solves:** Lets `/ask` work for anonymous visitors in dev/preview without a Clerk account, while defaulting to auth-required in production automatically — no human has to remember to flip an env var per environment.
- **Alternatives that existed:** Require auth everywhere unconditionally with a test-only bypass; infer environment purely from `FLASK_ENV` without the `VERCEL` fallback; a single decorator with a `required=` parameter instead of two named functions.
- **Why this won:** Fail-safe-by-platform-detection — `VERCEL=="1"` is set automatically by the runtime, not something a developer must remember. `tests/conftest.py:21` explicitly forces `CLERK_ENFORCE_AUTH=false` for tests, confirming intent: local/test permissive, deployed strict.
- **What would break if removed:** If `CLERK_ENFORCE_AUTH` always defaulted false, anonymous callers could submit `/ask` questions consuming AI budget with no accountable `user_id`.
- **Follow-up questions I should be able to answer:**
  - Does `require_clerk_auth` ever respect `CLERK_ENFORCE_AUTH`? No — it's unconditional; the env var only affects `maybe_require_clerk_auth` call sites.
  - The two `/ask` implementations duplicate this enforcement check independently (decorator vs. inline `if`) — what guarantees the two stay in sync? Nothing structural.

## `CLERK_AUDIENCE` gap — precisely what's exposed and under what conditions

- **What was chosen:** Audience (`aud`) verification is conditionally skipped: `verify_aud: False` whenever `CLERK_AUDIENCE` is unset — the default, since `.env.example` ships it blank.
- **Where it lives:** `backend/auth.py:57-65`; `.env.example:56-63` names this exact gap and calls out `/api/user/delete-account`; `tests/test_auth.py:143-165` deliberately unit-tests this default behavior, i.e. it's an intentional, known default, not an oversight nobody noticed.
- **Problem it solves (or rather, doesn't fully solve):** Without a template mismatch, Clerk's default session token often omits `aud` or sets it to something unopted-into, so requiring `CLERK_AUDIENCE` unconditionally would break the out-of-the-box frontend flow, which calls `getToken()` with no template argument.
- **What the gap actually is, precisely:** Any JWT that's correctly RS256-signed by Clerk's own JWKS and has a valid, matching `iss` is accepted, **regardless of what audience it was minted for**. A classic missing-audience-restriction weakness, not an impersonation weakness — the `sub` claim still identifies exactly one real Clerk user; RS256 signature verification still holds. What it *does* allow is a token minted for a *different relying party under the same Clerk issuer* being replayed against this backend as if it were a normal Sh'elah session token.
- **Exact endpoint exposed, and every other endpoint sharing the same exposure:** `.env.example` singles out delete-account as "the sole authorization gate," but the same verification path backs *every* Clerk-authenticated route — data export, user preferences, semantic bookmarks, ask-history read/delete, and the devtools routes. Delete-account is simply the highest-severity one because it's irreversible and cascades to a real Clerk identity deletion.
- **Real-world exploitability condition:** Requires another surface sharing the exact same `CLERK_JWT_ISSUER` (same Clerk "application" instance) that mints tokens with a different, unchecked audience. No second such surface was found in this codebase — the gap is currently latent, not actively exploitable. But it's a **silent trap**: nothing fails loudly when `CLERK_AUDIENCE` is unset — no startup warning, no log line — so the moment a second Clerk-authenticated surface is added under the same instance (a common move — an admin dashboard, a native app on the same Clerk project), this becomes exploitable with zero code change or warning.
- **Alternatives that existed:** Fail closed — refuse to serve auth-required routes if `CLERK_AUDIENCE` is unset in a detected-production runtime (mirrors the `CLERK_ENFORCE_AUTH` prod-auto-detect pattern already used elsewhere). Require `CLERK_AUDIENCE` unconditionally and force operators to configure a JWT template.
- **Why the current approach "won" (or wasn't fixed):** Flagged as a known, accepted, still-outstanding risk — the comment is written in present tense as guidance, not documentation of a completed mitigation.
- **Follow-up questions I should be able to answer:**
  - Is `CLERK_AUDIENCE` actually set in production right now? Unverifiable from the repo — needs a direct answer from the Vercel dashboard.
  - If a second Clerk-linked surface is added later, what's the process for setting `CLERK_AUDIENCE` before it ships, given nothing in CI or code enforces it? None currently exists.

## App-layer auth vs. Postgres RLS — is RLS real defense-in-depth, or decorative?

- **What was chosen:** Two parallel, inconsistent data-access strategies: (A) a service-role client (bypasses RLS entirely) used with hand-written `.eq("user_id", ...)` filters — the majority of routes, including the two most sensitive (export, delete); (B) a user-scoped client that forwards the caller's bearer token so Postgres RLS evaluates `auth.uid()::text = user_id` — only `user_preferences` and `semantic_bookmarks`, gated by a hardcoded `STRICT_SUPABASE_RLS = True` (`app.py:900`).
- **Whether it actually functions as defense-in-depth — the central finding: it does not, for most data access, and it's questionable whether it functions at all where it's wired up.**
  1. **Path A never touches RLS** — service-role bypasses it by Supabase's own design. Authorization there is 100% the hand-written filter in Python. If that filter were ever omitted in a future edit, RLS would not catch it.
  2. **Path B is itself questionable at runtime.** The module docstring in `routes_privacy.py:12-16` states outright that the user-scoped client "only works when the caller's bearer token is a Supabase-compatible JWT (a Clerk JWT minted from the 'supabase' token template) — the frontend's `authHeaders()` sends a plain Clerk session token." `docs/DATABASE.md:116` independently confirms the same uncertainty. Whether `user_preferences`/`semantic_bookmarks` work at all for real users depends on undocumented Supabase-dashboard configuration this repo can't confirm — and the team's own code deliberately routed the two highest-stakes endpoints (export, delete) around this path entirely rather than fixing it.
  3. **`user_memories` RLS blocks all client-side access by design** — service-role is the only way in, not a fallback.
- **Alternatives that existed:** Configure Supabase's native Clerk third-party-auth support properly and route everything through the RLS-scoped client, making RLS the real, single enforcement layer. Abandon RLS-as-primary-defense formally, treat server-side filtering (backed by tests asserting the filter is present) as the actual boundary, keep RLS only where it happens to work.
- **Why the current split "won":** Reads as organic accretion under time pressure — `routes_privacy.py`'s docstring is explicit that Path B was tried and rejected for DSR routes specifically because it 403'd real users, strongly implying the RLS-scoped path was found broken/fragile in practice and the fix was "route around it," not "fix the bridge."
- **What would break if RLS policies were removed entirely:** For Path A routes, nothing observable — the app-layer filter is already doing 100% of the authorization work. For Path B, if it's not currently functioning as intended, removing the policies wouldn't newly break anything either.
- **Follow-up questions I should be able to answer:**
  - Has this been verified end-to-end against the actual deployed project — does a real signed-in user's plain Clerk token currently succeed or 403 against `user_preferences`/`semantic_bookmarks`? Answerable by calling `GET /api/devtools/rls-audit` while signed in, but that only reports whether a client object was constructed, not whether PostgREST actually accepted the token on a real query.
  - If RLS isn't reliably functioning, is `STRICT_SUPABASE_RLS = True` the right failure mode (403 to real users), or should those two routes migrate to the service-role + explicit-filter pattern like everything else?
  - Why does production code not use `scripts/clerk_supabase_rls.py`'s more defensive bridging implementation — is it dead/reference code, or run manually as part of `scripts/verify_integrations.py`?

## Service-role bypass for DSR/privacy endpoints (deliberate, not an oversight)

- **What was chosen:** `export_user_data` and `delete_account` explicitly use the service-role client with hand-written `.eq("user_id", user_id)` filters on every table, rather than the RLS-scoped client, and the module docstring says so in plain language.
- **Where it lives:** `backend/routes_privacy.py:85-158`, gated by `@require_clerk_auth`.
- **Problem it solves:** The RLS-scoped client would 403 for real users given the token-template mismatch above — service-role + explicit filter is the only way these compliance-critical endpoints (GDPR access/portability/deletion, `docs/PRIVACY_OPERATIONS.md:12-38`) actually work for every real user.
- **Alternatives that existed:** Fix the Clerk↔Supabase JWT bridge first, then use the RLS-scoped client uniformly. Keep RLS-scoped access with a service-role fallback only on 403.
- **Why this won:** Pragmatism — correctness for the compliance-critical path prioritized over architectural purity, stated explicitly in the docstring.
- **What would break if switched to the RLS-scoped client:** Every delete-account/export call from a real signed-in user would likely fail with 403/503 given the currently-unverified JWT-acceptance state — materially worse for a GDPR-facing feature.
- **Follow-up questions I should be able to answer:**
  - What stops a future contributor from "fixing" this by swapping in the RLS-scoped client without first confirming the bridge actually works, silently reintroducing the exact failure this was written to avoid?
  - Is there a test that would catch a regression where the `.eq("user_id", ...)` filter is accidentally dropped from one of these queries? Not found.

## Delete-account ordering: Supabase-first, Clerk-identity-last

- **What was chosen:** `delete_account` deletes every Supabase table row first; only if all table deletes succeed does it call Clerk's Backend API to delete the identity itself.
- **Where it lives:** `backend/routes_privacy.py:196-257`.
- **Problem it solves:** Prevents orphaned data with no recovery path — if the Clerk identity died first and a Supabase delete then failed, the user would be locked out with data still present and no way to log back in and retry.
- **Alternatives that existed:** Delete Clerk identity first (simpler, creates orphan risk). Two-phase/saga with a durable job queue (more robust, more infra). Soft-delete/tombstone deferred to the retention job.
- **Why this won:** For a solo-developer operation with no distributed-transaction infrastructure, sequential-with-explicit-idempotency (both table deletes and the Clerk call are documented as idempotent) is the simplest approach avoiding orphaning without new infra.
- **What would break if the ordering were reversed:** A partial-failure scenario permanently stops the user from retrying — no way to re-authenticate to call delete-account again, and no admin tooling to manually finish the job.
- **Also worth noting:** If `CLERK_SECRET_KEY` is unset, Supabase data is fully wiped but the Clerk login identity is never deleted, reporting `clerk_deleted=false` with HTTP 207. Documented, but nothing forces `CLERK_SECRET_KEY` to be present before accepting delete-account calls in production — it fails soft per-request, not at boot.
- **Follow-up questions I should be able to answer:**
  - Is `CLERK_SECRET_KEY` actually configured in production? If not, every "delete my account" request today wipes Supabase data but leaves the login (and the ability to sign back into a now-empty account) alive indefinitely.

## Cron-route authorization (`CRON_SECRET`)

- **What was chosen:** `/api/devtools/budget-check` and `/api/devtools/retention-enforce` are gated by `hmac.compare_digest` on `Authorization: Bearer <CRON_SECRET>`, failing **closed** (503) if unset.
- **Where it lives:** `backend/routes_devtools.py:263-285`, `backend/routes_privacy.py:272-314`, `vercel.json`.
- **Problem it solves:** These routes perform real destructive/financial actions but aren't naturally tied to any Clerk user identity (a cron job has no "current user").
- **Alternatives that existed:** IP-allowlist Vercel's cron source IPs. Require Clerk-authenticated admin role (not available — no role model exists in this app at all). Plain string comparison (timing-attack-vulnerable).
- **Why this won:** A shared secret with constant-time comparison, fail-closed-on-unset, is the standard low-infra pattern; Vercel natively sends `CRON_SECRET` as a bearer token once configured.
- **What would break if removed:** Anyone discovering these paths could trigger bulk deletion of history/usage rows or spam the budget-check logic — nothing else prevents it.
- **Follow-up questions I should be able to answer:**
  - Is `CRON_SECRET` actually set in production? If not, both cron endpoints are permanently 503, silently disabling both the AI-spend guardrail and retention enforcement.

### Suspicious / thin areas — auth & access control

This is likely the single weakest area of the app, and it's not hidden — the code and `.env.example` half-admit most of it. The actual attack surface is more specific than the comments spell out:

1. **RLS may not be functioning as a defense-in-depth layer at all today, for either path** — detailed above. Needs verifying against the live deployment (calling `PUT /api/user/preferences` as a real signed-in user in production and confirming 200 vs 403/500 is the fastest check), not assumed either way.
2. **`CLERK_AUDIENCE` is unset by default with no runtime warning anywhere** — not even on `/api/stack/health`, which reports `clerk.configured`/`clerk.enforced` but not whether audience verification is active. An operator checking that endpoint has no way to notice this gap is live.
3. **No session/token revocation check.** JWT verification only checks signature, issuer, expiry, and optionally audience — never whether the session has been revoked or the user banned server-side. Standard JWT tradeoff, but undocumented anywhere in this codebase as an accepted one.
4. **No admin/role distinction anywhere in the auth model.** `require_clerk_auth` only proves "a valid signed-in Clerk user," nothing more. Several devtools diagnostic routes (`stack/health`, `reliability`, `rls-audit`) are gated by that alone — meaning any signed-up user, not just an operator, can learn configuration/rate-limit/RLS-status details about the deployment.
5. **Two independent `/ask` auth implementations kept in sync by hand** — a decorator on one, an inline check on the other, currently equivalent but nothing enforces that.
6. **`CLERK_SECRET_KEY` absence is a silent per-request partial-failure, not a boot-time hard requirement**, despite gating the actual identity-deletion step of a GDPR-relevant flow.
7. **A cookie-based Supabase-token fallback exists** (`app.py:1008-1038`, reading `sb-access-token`/`supabase-access-token`/chunked cookies) that Clerk's own auth doesn't set — looks like leftover/parallel support for a native-Supabase-Auth flow predating the Clerk consolidation. Worth confirming whether anything still sets these cookies or whether it's dead code.
8. **`tests/test_auth.py` mocks `jwt.decode`/`PyJWKClient` entirely** — verifies orchestration logic, never exercises a real RS256 signature check or a real JWKS fetch end to end.

---

# 5. Frontend architecture

## Flask/Jinja server-rendered HTML vs. a JS framework SPA

- **What was chosen:** The entire main app (search, reader, sidebars, settings, calendar, prayer books, library index) is one Jinja template, `templates/index.html`, rendered server-side by a single Flask route and hydrated by a ~10,300-line inline `<script>` block plus a handful of ES modules loaded at the bottom.
- **Where it lives:** `app.py:1361-1379` (routes → `render_template("index.html", ...)`); `templates/index.html` is 12,214 lines / 626KB total, primary logic script spanning lines 1883–12204 (confirmed no `type="module"` on that tag).
- **Problem it solves:** One Python process, one Vercel serverless function, no separate frontend build/deploy pipeline, no client-server API contract to version, no hydration-mismatch bug class (there's no hydration — the browser owns the DOM from the start).
- **Alternatives that existed:** (1) Next.js/React SPA — requires a Node build step and either duplicating backend logic as API routes or maintaining a fetch contract; on Vercel this usually means two deploy artifacts instead of one. (2) htmx — would let the existing Jinja/Flask backend return HTML fragments, cutting client JS considerably; not adopted (zero `hx-` attributes anywhere) even though arguably a closer natural fit than what exists. (3) A static SPA hitting a pure JSON API — cleanest separation, doubles deploy surface for a two-person-scale project.
- **Why this won:** Solo/small-team velocity and Vercel serverless economics for a Python backend — one deploy target, one cold-start path, no Node build step gating every deploy. First paint is server-rendered. Tradeoff: "SPA-like" state lives entirely in globals inside one script tag rather than any component framework's state model.
- **What would break if removed:** No client-side router or component tree to fall back to — this *is* the app. Migrating to a framework would mean rewriting the ~10,300-line inline script from scratch; nothing in it is portable as-is (built entirely on `document.getElementById` + globals).
- **Follow-up questions I should be able to answer:**
  - Why is `index.html` one template instead of using Jinja `{% include %}`/`{% extends %}` the way the legal pages do?
  - What SEO/first-paint benefit does server rendering actually buy for an app that's mostly a search interaction, not content pages?
  - If this needs to become a React app later, what's the migration unit — page-by-page, or a full shell rewrite?

## Vanilla ES modules with no bundler, plus a `window.ShelahModules`/`window.ShelahMotion` bridge

- **What was chosen:** Six small ES modules (`state.js`, `ai-service.js`, `reader-ui.js`, `zmanim.js`, `motion.js`, `main.js`) loaded via plain `<script type="module">` tags — no webpack/vite/esbuild/rollup anywhere in the repo; `node_modules` contains only `tailwindcss`, `@tailwindcss/typography`, `@sentry/browser` as devDependencies, not a bundler.
- **Where it lives:** `static/js/main.js:1-33` builds `window.ShelahModules = {...}` (line 18); `static/js/motion.js:214-226` does the same for `window.ShelahMotion`, with an explicit comment: "Expose on window so legacy inline-script code can call without an import."
- **Problem it solves:** The giant classic `<script>` runs in global scope, not module scope, so it cannot `import` from the ES modules directly — the `window.Shelah*` objects are the only bridge.
- **Alternatives that existed:** A real bundler letting the legacy script itself become a module — never adopted for JS (the CSS build-step question was explicitly resolved via a prebuilt Tailwind output; the JS bundler question never got the same treatment). Converting the whole legacy script to `type="module"` — would fix scoping for free without a bridge object, but everything in it currently assumes global scope.
- **Why this won:** Zero build-step friction for a project without dedicated frontend headcount. Third-party deps are all CDN-loaded with SRI hashes anyway, so there was never real bundling of vendor code either.
- **What would break if removed:** `window.ShelahModules` — nothing today, because nothing consumes it (see Suspicious). `window.ShelahMotion` — real call sites exist (three `?.animateIn(...)` calls in the inline script).
- **Follow-up questions I should be able to answer:**
  - `window.ShelahModules` is built but has zero consumers in `index.html` — dead bridge, or an unfinished migration seam?
  - Given the CSP already ships `unsafe-inline` because of the legacy inline script, what benefit does splitting logic into ES modules provide today versus just adding to the classic script?
  - What would it take to make the classic script itself `type="module"`, eliminating the bridge pattern entirely?

## Tailwind (build-step utility framework) coexisting with hand-written component CSS

- **What was chosen:** A layered stack, loaded in this order: `tokens.css` → `style.css` (4,153 lines, original hand-written base layer) → feature sheets (`typography.css`, `reader.css`, `sidebar.css`, `halacha.css`, `prayer.css`, `ai.css`, `calendar.css`, `loading.css`, explicitly commented "supplement style.css; do not replace") → `tailwind.css` last, "so utilities override custom CSS." Tailwind is compiled offline (`npm run build:css`) into a committed, minified 52KB file, purge-scoped to templates + JS. **Zero `@apply` usage anywhere** — Tailwind is exclusively utility classes in markup; hand-written CSS is exclusively plain CSS using `var(--token)`.
- **Where it lives:** `static/css/tailwind.input.css` (3-line stub), `tailwind.config.js`, `templates/index.html:78-90` (load order).
- **Problem it solves:** Utility classes handle one-off layout/spacing fast; hand-written CSS handles cross-cutting component state (skeleton shimmer, reader typography, sidebar animation) that would be unreadable as long utility-class strings across 12,000 lines.
- **Alternatives that existed:** Pure Tailwind everywhere — rejected, the reader/AI/calendar features have genuinely stateful, animated, theme-aware styling that would be extremely painful as raw utility strings. Pure hand-written CSS — would mean hand-writing every layout/spacing/breakpoint rule across a 12K-line template; Tailwind clearly won that fight for non-stateful markup.
- **Why this won:** Either layer can be audited independently since they never mix inside one rule — grepping for arbitrary-color Tailwind usage finds token-bypass in markup, grepping for raw hex in feature sheets finds token-bypass in hand-written CSS, and the two searches don't interfere.
- **What would break if removed:** Removing Tailwind means rewriting every layout utility by hand across 12,214 lines. Removing the hand-written sheets loses the token-driven dark theme for anything not expressible as a utility class.
- **Follow-up questions I should be able to answer:**
  - Why does `tailwind.css` load *last*, after the hand-written feature sheets, rather than first as a reset/base layer?
  - Given the strict split, why does `index.html` contain 14+ instances of arbitrary-value Tailwind classes like `bg-[#faf7f0]` that hardcode colors, bypassing both the token layer *and* Tailwind's own config-based palette?
  - Is `npm run build:css` run before every deploy, and what happens if a hand-written sheet changes but nobody rebuilds?

## The `tokens.css` custom-property system for theming/dark mode

- **What was chosen:** A single 670-line file, the sole source of truth for every themeable value, declared three times: light defaults on bare `:root`, an explicit-toggle dark block under `[data-theme="dark"]`/legacy class selectors, and an OS-preference-fallback dark block under `@media (prefers-color-scheme: dark)`. An inline head script reads `localStorage` and stamps `data-theme` on `<html>` before first paint to avoid a flash of the wrong theme.
- **Where it lives:** `static/css/tokens.css:1-670`, loaded first of all stylesheets. The file's own header comment states the prior state it replaced: "574 scattered `body.theme-dark` overrides across 9 files."
- **Problem it solves:** Consolidates dark-mode overrides into one place; also solves FOUC since the theme decision happens synchronously before paint.
- **Alternatives that existed:** A CSS-in-JS/framework theming library (irrelevant without a framework; Tailwind's `dark:` variant isn't used here either — theming is 100% custom-property-driven). Per-component dark overrides — explicitly the thing being migrated away from.
- **Why this won:** Native custom properties require no JS to repaint on theme change, and work identically for hand-written CSS and (via remap tokens) select Tailwind utilities.
- **What would break if removed:** Every dark-mode value disappears at once — a genuine single point of failure, which is the design goal, but also means a bug here has app-wide blast radius.
- **Follow-up questions I should be able to answer:**
  - Why maintain three separate declarations of the same ~150 tokens instead of one dark block reused via indirection?
  - `body.theme-dark` is called out as a "migration alias" kept so untouched selectors keep working — how much of the codebase still depends on the class instead of the attribute, and is there a plan to finish that migration?

### Suspicious / thin areas — frontend architecture

- **`window.ShelahModules` bridge appears to be dead code.** `main.js` builds it and assigns it to `window`, but a repo-wide grep for `ShelahModules.` inside `index.html` returns zero matches. `ai-service.js`'s `askAi()` and `reader-ui.js`'s `saveSemanticBookmark()` both have parallel, independent implementations inline in the giant script instead — `reader-ui.js` even contains a self-aware comment that the main shell "owns" bookmarking and the module skips binding there to avoid duplicate handlers. By contrast, `zmanim.js`'s exports are called only from `main.js`, so that one might be genuinely load-bearing. Net effect: the ES-module migration looks started (`motion.js` finished) and stalled (the rest built a bridge the inline script was never updated to call through) — worth a straight answer on whether that's planned or abandoned.
- **`npm run build:css` is not wired into CI or deploy.** `.github/workflows/ci.yml` has zero Node/npm steps; `vercel.json` has no `buildCommand` override. The compiled `tailwind.css` is a manually built, committed artifact. Real staleness risk: a template change adding new Tailwind classes, or a config change, silently drifts from deployed CSS until someone notices visually — no CI step rebuilds and diffs.
- **The mandated "no `bg-[#…]` in loading UI" rule is violated in the loading skeleton itself.** The "Popular Texts" skeleton hardcodes `bg-[#fffaf1]`/`border-[#ddd4c3]` instead of the semantic `--load-skeleton-*` tokens — won't repaint correctly in dark mode the way a token reference would.
- **112+ inline `onclick=` handlers and 40+ inline `style=` attributes are a documented, load-bearing blocker to CSP hardening**, not just style debt — `backend/helpers.py:77-88` explicitly ties the app's `unsafe-inline` CSP directives to this. Current counts (115 `onclick=`, 40 ` style=`) have grown since the code's own last-audited count, not shrunk. This is a real, currently-shipping security-posture cost the team is aware of and has deferred.
- **`index.html` has zero Jinja componentization; only the legal pages do.** The single highest-traffic, highest-complexity page in the app is also the least factored.
- **`static/style.css` (4,153 lines) is effectively a fourth, unlabeled CSS layer** predating the later tokens/feature-sheet split, and should be accounted for explicitly when explaining the CSS architecture rather than treated as legacy filler.

---

# 6. Deployment / infrastructure

## Vercel serverless as the deploy target

- **What was chosen:** The whole app runs as a single Python serverless function, entered via `api/index.py`, with `functions.api/index.py.maxDuration: 90` in `vercel.json`.
- **Where it lives:** `vercel.json`, `api/index.py`, `docs/RUNBOOKS.md:40-65`, `plan.md:751-865`.
- **Problem it solves:** Zero server/container to provision, patch, or scale; deploy is `git push`. Fluid compute means a paused instance between requests costs nothing.
- **Alternatives that existed:** A long-running host (Render, Fly.io, Railway, a plain VM) running `uvicorn` continuously — documented in `README.md:205-209` as the literal self-hosted fallback, a known option left as an escape hatch, not overlooked.
- **Why this won:** No idle-server cost for a low/bursty-traffic Q&A app; git-push deploy with no separate CD pipeline; Provisioned Memory only bills while a request is in flight.
- **What would break if removed:** Losing serverless would require a persistent process to keep rate-limit counters, circuit-breaker state, and JWKS caches warm — most of which currently rely on being ephemeral and degrade (mostly) safely.
- **Follow-up questions I should be able to answer:**
  - What actually dominates the Vercel bill? Provisioned Memory, not Active CPU — `/ask` is I/O-bound and bills wall-clock while waiting on Gemini/Claude/Sefaria.
  - What happens to in-memory rate-limit counters and circuit-breaker state on a cold start? They reset to zero — known, documented.

## FastAPI-wraps-Flask via WSGIMiddleware

- **What was chosen:** `asgi.py` defines exactly one native async route (`POST /ask`) plus a health endpoint, then mounts the entire legacy Flask app underneath: `fastapi_app.mount("/", WSGIMiddleware(flask_app_module.app))`, exporting `app = fastapi_app` as the actual ASGI callable Vercel serves.
- **Where it lives:** `asgi.py:1-6` (docstring: "incremental async migration... keeps the existing Flask app intact"), `asgi.py:26,730,733`.
- **Problem it solves:** `/ask` is the one latency-critical, I/O-heavy path — parallel Sefaria fetch, RAG assembly, two AI providers with fallback. Real `async`/`await` with `asyncio.gather` lets independent I/O run concurrently on one event loop instead of blocking Flask's synchronous worker model. The other ~48 routes are low-traffic, mostly synchronous CRUD where a full async rewrite would have been pure risk for no latency win.
- **Alternatives that existed:** Rewrite everything in FastAPI, delete Flask. Run Flask directly on Vercel, keep `/ask` synchronous. Split `/ask` into its own separate function/service.
- **Why this won:** Git history shows this was explicitly incremental (commit `bb94376`, "switch Vercel to ASGI entrypoint," predates the full async `/ask` buildout) — a full rewrite of 48 routes was more risk than the one route that actually needed it. `docs/SERVICE_ARCHITECTURE.md:189` states the governing constraint: single-threaded asyncio event loop, no blocking I/O on it — also a **billing** rule, since blocking the loop defeats Fluid's concurrency discount.
- **What would break if removed:** Already happened once — commit `a55a42a` fixed a production bug where the ASGI `/ask` handler silently dropped `ai_cited_sources` that the Flask path had. That's the exact failure mode of this architecture: two implementations must stay behaviorally identical, and only the FastAPI one is reachable in production.
- **Follow-up questions I should be able to answer:**
  - Which `/ask` implementation does Vercel traffic actually hit? FastAPI's — native routes are matched before the WSGI mount fallback.
  - What does `asyncio.gather` buy here that Flask couldn't? True concurrent I/O across primary-source collection, Halachipedia/Wikipedia search, community-knowledge retrieval, and user-memory fetch.
  - Does the rate limiter still exist independently in each layer? No, not since `plan.md` §16.9 (2026-08-22) — `backend/rate_limit.py`'s `RateLimitMiddleware` is now the single ASGI-level middleware installed above both the native FastAPI routes and the Flask app mounted via `WSGIMiddleware`, so it observes 100% of traffic exactly once regardless of which framework ultimately serves the request. Flask-Limiter and the old ASGI-only in-process limiter are both deleted. See the rate-limiting section below for the current shape.

## `maxDuration: 90` and its coupling to `AI_TOTAL_BUDGET_SECONDS`

- **What was chosen:** `vercel.json`'s `maxDuration = 90`; `AI_TOTAL_BUDGET_SECONDS` defaults to 45, a single shared wall-clock budget for the AI-synthesis leg, enforced via `asyncio.wait_for`.
- **Where it lives:** `vercel.json`; `backend/claude.py:93-97` (comment: "Must stay under functions.maxDuration... one shared constant for both app.py and asgi.py so the two transports can't drift"); `asgi.py:436-462`.
- **Problem it solves:** Without a budget strictly under `maxDuration`, Vercel hard-kills the function mid-AI-call and the user gets a raw platform timeout instead of the app's own graceful fallback.
- **Alternatives that existed:** No per-request timeout, rely solely on `maxDuration` (risks truncation with no graceful message). A single constant duplicated by name in both files instead of one canonical import (the actual approach avoids the exact class of bug that already bit this codebase once — the `ai_cited_sources` drift).
- **Why this won:** `git log -p vercel.json` shows `maxDuration` was deliberately bumped 60→90 in the same commit whose message says "Coordinate timeout budgets across client/server/provider so requests no longer abort mid-retry."
- **What would break if removed:** Nothing enforces `AI_TOTAL_BUDGET_SECONDS < maxDuration` in code — it's a comment-only invariant. If someone sets the env var above 90 in the Vercel dashboard, the platform kills the function before the app's own timeout fires, silently reverting to no-graceful-fallback.
- **Follow-up questions I should be able to answer:**
  - Is the 45s/90s relationship actually correct today? Yes — 45 is exactly half of 90, giving margin for the rest of the request plus the fallback path; `plan.md:779` ratifies the current number.
  - What numeric constant looks inconsistent with this budget? `MODEL_REQUEST_TIMEOUT_SECONDS = 50` (per-provider SDK timeout) is *larger* than the 45s outer budget — the outer budget always cancels first, so 50 is currently dead configuration, not a live safety net.

## Single-region (`iad1`) deployment + Vercel Cron for budget-check and retention-enforce

- **What was chosen:** `regions: ["iad1"]` (single region); two daily Vercel Cron jobs an hour apart hitting `/api/devtools/budget-check` and `/api/devtools/retention-enforce`.
- **Where it lives:** `vercel.json`; `backend/routes_devtools.py:263-286`; `backend/routes_privacy.py:272-314`.
- **Problem it solves:** `iad1`/`pdx1`/`cle1` are the three cheapest Vercel regions; a single region avoids paying to warm several, with an explicit caveat in `plan.md:780` to revisit if the user base skews non-US-East. The crons: `budget-check` alerts on aggregate daily spend (not a ceiling — the docstring is candid that this is "after-the-fact ALERT... not the enforcement CEILING"); `retention-enforce` actually deletes rows past retention windows so the privacy policy's retention promise is code-enforced, not documentation-only.
- **Alternatives that existed:** Multi-region (rejected on cost, explicitly). A separate worker/queue system instead of cron hitting the same serverless function.
- **Why this won:** Vercel Cron needs zero new infrastructure — a config block hitting routes already in the deployed function. Both routes fail closed (503) if `CRON_SECRET` is unset, a deliberate security-audit fix.
- **What would break if removed:** Without `budget-check`, `DAILY_BUDGET_USD` alerting never fires. Without `retention-enforce`, the privacy policy's retention promise becomes documentation-only again.
- **Follow-up questions I should be able to answer:**
  - Does `budget-check` actually do anything right now? Per `docs/RUNBOOKS.md:35-38`, spend caps were "not yet configured by the project owner" as of the doc's date — it fires daily, no-ops, returns. Wired but administratively inert until `DAILY_BUDGET_USD` is set.
  - Why does this deployment have cron jobs at all, when `plan.md` elsewhere argues against them? See Suspicious below — a real, unresolved tension in the project's own planning doc.

## Environment variable strategy: REQUIRED vs. OPTIONAL, fail-closed vs. fail-open by design

- **What was chosen:** `.env.example` splits into REQUIRED (no working default — a whole feature silently no-ops without it) and OPTIONAL (every value has a working default, stated inline).
- **Where it lives:** `.env.example:1-105` in full.
- **Problem it solves:** A new operator or fresh Vercel import can tell at a glance which vars are launch-blocking versus tunable-later, without reading source — and *why* each optional var is optional, including security-relevant defaults like `CLERK_AUDIENCE`.
- **Alternatives that existed:** A flat, undifferentiated list (common default). A runtime startup check that hard-fails on missing required vars (more robust, not implemented — missing values currently cause silent no-ops, not crashes).
- **Why this won:** Cheap, self-documenting, matches how Vercel's dashboard env-var UI works.
- **What would break if removed:** Nothing breaks immediately — that's the risk. There's no startup assertion catching a missing required var; a feature just silently degrades.
- **Follow-up questions I should be able to answer:**
  - What happens if `SUPABASE_SECRET_KEY` is never set? Every Supabase-backed feature degrades silently — budget enforcement fails open ("a Supabase hiccup degrades to uncapped for now, not a 500 on /ask") and retention-enforce 503s.
  - Why does `CLERK_ENFORCE_AUTH` stay an env var instead of a code constant? It needs to differ per environment by design, unlike the tuning constants folded into code.

## Dependency pinning: loose top-level pins vs. hash-locked lock files (CI-only)

- **What was chosen:** `requirements.txt`/`requirements-dev.txt` are hand-maintained exact-version direct dependencies with dated "sync status" comments. Separately, fully hash-locked `requirements.lock.txt`/`requirements-dev.lock.txt` are generated via `uv pip compile --generate-hashes` and installed **only in CI** with `--require-hashes --only-binary=:all:`.
- **Where it lives:** `requirements.txt`, `requirements.lock.txt`, `.github/workflows/ci.yml:20-30`.
- **Problem it solves:** Closes a supply-chain finding — CI previously installed unpinned transitive dependencies with no hash verification, meaning a compromised/typosquatted transitive package could execute arbitrary code at install time via a malicious sdist build-backend.
- **Alternatives that existed:** Poetry/PDM with a full lockfile as sole source of truth instead of hand-edited `requirements.txt` alongside a generated lock file. Dependabot/Renovate for automated bump PRs (not present — bumps are manual).
- **Why this won:** Minimal-diff security fix bolted onto the existing workflow without switching dependency managers.
- **What would break if removed:** Reverting to unpinned-transitive CI installs reopens the closed finding. **Production, notably, has never had this protection** — see Suspicious below.
- **Follow-up questions I should be able to answer:**
  - Does Vercel's production build use the hash-locked files? No — only CI does; Vercel installs from `requirements.txt` directly, unpinned at the transitive level.
  - Does a CVE found by `pip-audit` block a merge? No — see Suspicious below.

### Suspicious / thin areas — deployment & infra

1. **The current `vercel.json` region/cron/memory config is entirely uncommitted at the time of this audit.** `git show HEAD:vercel.json` has no `regions` key and no `crons` key. The working tree and index already contain the region pin and both cron jobs, with the invalid `memory: 1024` key removed — none of this is in git history yet. **This should be committed before treating the region/cron decisions above as "shipped."**
2. **Direct contradiction with the project's own planning document.** `plan.md` states as a hard rule: "No cron jobs, no keepalive pings, no warmers... nothing in this repo should exist to keep an instance alive," restated in a checklist as "Cron Monitoring: Off." The actual (in-progress) `vercel.json` now has two. They arguably don't violate the *spirit* — once-daily admin jobs doing real work, not warmers — but `plan.md` was never updated to carve out that exception, and nothing documents the reconciliation.
3. **`MODEL_REQUEST_TIMEOUT_SECONDS = 50` is functionally dead configuration**, as detailed in the dual-LLM section — harmless today, but a number that looks intentional and isn't load-bearing.
4. **`AI_TOTAL_BUDGET_SECONDS` vs. `maxDuration` is a comment-enforced invariant, not a code-enforced one** — nothing asserts at startup that the env-overridable budget stays under the platform's hard limit.
5. **Resolved for the application-level middleware, still genuinely relevant for the edge WAF.** This item used to say per-process rate limiting was a known, unresolved gap with the single-region choice load-bearing for how bad it was. `backend/rate_limit.py`'s ASGI middleware is Redis-backed since `plan.md` §16.9 (2026-08-22) — no longer per-process, no longer dependent on the region pin. The **edge WAF** rate-limit rule (`plan.md` §16.3-L1, entered in the Vercel dashboard 2026-08-26) is a separate layer that genuinely does track counters per-region by Vercel's own design; the `regions: ["iad1"]` single-region pin remains a real, load-bearing synergy for that layer specifically — adopting multi-region later would still need a corresponding fix there, just not for the application-level middleware anymore.
6. **CI does not gate deployment, and two of CI's own checks don't gate the CI job either.** `ruff check` and `pip-audit` both run with `continue-on-error: true` — lint failures and known CVEs never block. Only pytest, the pre-commit (gitleaks + bandit) step, and SonarCloud are blocking. Nothing in the repo shows GitHub Actions gating the actual Vercel deploy — that coupling, if it exists, lives in GitHub branch-protection settings outside the repo.
7. **Production never gets the hash-locked, vulnerability-scanned dependency set** — all the supply-chain hardening described above protects the test environment, not the one serving traffic.
8. **`memory: 1024` sat in `vercel.json` doing nothing for an extended period** — invalid under Fluid compute per Vercel's own docs, silently ignored, only removed in the current uncommitted working tree.
9. **`budget-check` cron is currently a no-op in production** per `docs/RUNBOOKS.md` — fires daily, costs an invocation, does nothing observable unless `DAILY_BUDGET_USD` has since been set.
10. **Two independent `/ask` implementations must stay in sync by hand, and only one is reachable in production** — already caused one shipped bug; nothing structural prevents recurrence.

---

# 7. Caching, rate-limiting, and cost control

## Per-user AI spend ceiling (per-caller circuit breaker)

- **What was chosen:** A hard pre-call budget check keyed by Clerk `user_id` (or `ip:<addr>` for anonymous callers), enforced synchronously before every LLM dispatch.
- **Where it lives:** `backend/cost_meter.py:285-312` (`check_user_budget_and_enforce`), called from `asgi.py:668` before AI synthesis. Spend recorded via `record_llm_call()`, into Supabase `ai_usage_log`.
- **Problem it solves:** Without it, an authenticated (or anonymous, if enforcement is off) caller could hit `/ask` unboundedly, and the operator finds out from the invoice.
- **Alternatives that existed:** A database-level atomic counter/advisory lock instead of read-then-write from application code. Provider-side spend caps as an independent backstop. A pre-purchased token/credit ledger debited atomically per call.
- **Why this won:** Cheapest to build with the existing Supabase client, ships enabled by default (`$2.00`/day) specifically so the "no ceiling" gap wasn't left fully open while the real global-breaker fix (`plan.md` §16 Phase 9b) is deferred.
- **What would break if removed:** Any signed-in (or anonymous) user could call `/ask` unlimited times per day with zero cost ceiling.
- **Follow-up questions I should be able to answer:**
  - Is this check-then-spend atomic, or can concurrent requests race past it? Not atomic — see Suspicious.
  - Does the check cap a single call's cost, or only the running total? Only the running total.
  - What happens when the Supabase read fails? Fails open — a Supabase hiccup silently uncaps spend rather than blocking `/ask`.

## Global daily budget alert (not enforcement)

- **What was chosen:** A separate, opt-in, cron-triggered function summing the entire day's cost across all callers, firing an alert if it crosses `DAILY_BUDGET_USD`. Never blocks a request.
- **Where it lives:** `backend/cost_meter.py:171-224`, invoked by the `budget-check` cron.
- **Problem it solves:** The one signal for "the whole app is burning money," as opposed to any single user.
- **Alternatives that existed:** A real global circuit breaker checked before every provider call (exactly what `plan.md` §16.3 "L3" specifies and does not exist yet, per the function's own candid docstring). Provider-side hard spend caps as a true independent backstop.
- **Why this won:** The cheap half of a two-part design — alert now, enforce later, since a real breaker requires deciding what `/ask` degrades to.
- **What would break if removed:** Nothing functionally — but the only whole-app spend signal disappears; many low-and-slow callers each under their own ceiling could produce unbounded total spend with zero alarm if `DAILY_BUDGET_USD` is unset (it is, by default).
- **Follow-up questions I should be able to answer:**
  - Is `DAILY_BUDGET_USD` actually set in production? Defaults to a no-op if unset.
  - If it fires, does anything automatically throttle traffic? No — purely a human-reaction alert.

## Sefaria response cache (memory → disk → network)

- **What was chosen:** Three-tier read-through cache: in-process `TTLCache` (1hr) → on-disk JSON cache (`.sefaria_cache/`, MD5(url)-keyed, 7-day TTL) → live HTTP.
- **Where it lives:** `backend/sefaria_library.py:298-327` (`_cached_get`), disk tier `:111-140`.
- **Problem it solves:** Sefaria is the primary source-text provider for every `/ask` call and library browse; without caching, every request re-fetches near-static text, and Sefaria has shown it will 403-block this app under load.
- **Alternatives that existed:** A real bounded LRU/TTL disk cache library with size eviction. A shared cache surviving across concurrent serverless instances (Upstash Redis, Vercel Runtime Cache) instead of per-container state.
- **Why this won:** Zero new dependencies, trivial to reason about locally — Sefaria content is close to static, so a crude TTL is "good enough" in dev.
- **What would break if removed:** Every text lookup would hit Sefaria's network API directly, multiplying latency and 403-block risk.
- **Follow-up questions I should be able to answer:**
  - Does the disk cache actually persist in production? Almost certainly not — see Suspicious.
  - Is there a size bound? No — 27MB / 185 files locally already, with no eviction beyond passive TTL-staleness on read.

## Shared in-process TTL cache (`backend/cache.py`) — live, not dead code

- **What was chosen:** A single thread-safe `TTLCache` (LRU + TTL) as a shared utility, replacing ad-hoc per-module cache dicts. **Correction to a stale prior-session note:** this was orphaned as of Stage 7 but has since been wired in — confirmed live via imports in `backend/sefaria.py`, `backend/sefaria_library.py`, `backend/search.py`, `backend/zmanim_engine.py`, and covered by dedicated tests.
- **Where it lives:** `backend/cache.py:26-94`.
- **Problem it solves:** Sefaria title/ref resolution, wiki/Halachipedia/HebrewBooks search results, and Hebcal zmanim lookups all needed bounded in-process memoization; each previously had its own hand-rolled, eviction-free dict.
- **Alternatives that existed:** `cachetools`/`functools.lru_cache` (no native TTL). A shared external cache from the start.
- **Why this won:** No new dependency, ~70 lines, replaced duplicated logic across 4 modules — a genuine consolidation win.
- **What would break if removed:** All four modules would need their own reimplementation or lose caching entirely.
- **Follow-up questions I should be able to answer:**
  - Is this the same layer as the Sefaria disk cache? No — purely in-memory; the disk tier is a separate hand-written layer behind it.
  - Is `backend/ask_pipeline.py` (built alongside this in the same prior session) also live now? No — still fully orphaned, unrelated to cost control but worth knowing as a still-dead sibling.

## Rate limiting — unified ASGI middleware, Redis-backed shared store, asymmetric fail-open/fail-closed

> **Updated 2026-08-26** to reflect `plan.md` §16.9's 2026-08-22 unification (this section previously described the pre-unification dual-limiter state; that history is preserved in git blame, not repeated here).

- **What was chosen:** One `RateLimitMiddleware` (Starlette `BaseHTTPMiddleware`), installed once on `asgi.fastapi_app`, above both the native FastAPI routes and the Flask app mounted underneath via `WSGIMiddleware` — the single point in the process that observes 100% of traffic exactly once, regardless of which framework ultimately handles it. One policy table (`_POLICIES`, keyed by route class: `llm`, `heavy`, `fanout`, `feedback`, `telemetry`, `cheap`), one store abstraction, one key function, one 429 body shape, one `Retry-After` header.
- **Where it lives:** `backend/rate_limit.py` (the full module — `_POLICIES`, `_check()`, `RateLimitMiddleware`), installed in `asgi.py`. Flask-Limiter and the old hand-rolled `asgi.py`-only in-process limiter are both deleted; every remaining `Flask-Limiter`/`flask_limiter` reference in the codebase is a historical code comment, not a live dependency.
- **Problem it solves:** The original problem (an unthrottled caller defeating the per-user dollar budget by request rate alone) plus the two defects this file used to document: the "two independently-maintained limiters, no shared source of truth" divergent-duplication risk, and the per-process-counter defect that made the old limit non-global on Vercel Fluid Compute.
- **Store:** Upstash Redis over `rediss://`, one shared counter across every concurrent instance and every cold start — the direct fix for the per-process-counter defect. Falls back to an in-process store (the old defect, reintroduced) only when `RATE_LIMIT_REDIS_URL` is unset, logging a loud boot-time warning naming the gap. **Confirmed set in the live Vercel production environment (operator-confirmed 2026-08-26, `plan.md` §31.2)** — not independently re-verified against `/api/stack/health`'s `security.limiter_store` field from this file's own audit, but confirmed directly by the repo owner.
- **Failure posture — deliberately asymmetric.** Every class except `llm` fails **open** (`fail_open=True` — a reader shouldn't be blocked because Redis blipped); the `llm` class (`/ask`) fails **closed** (`fail_open=False` — an unmetered LLM call during a store outage is a budget hole, not a degraded feature). Both paths alarm to Sentry via `_capture_backend_error`. This asymmetry was originally shipped untested — `tests/test_rate_limit_middleware.py` (added 2026-08-26, closing `plan.md` §31.1, the single highest-priority gap this file used to flag) now stubs the store to raise `_StoreUnavailable` and asserts both postures plus the Sentry call directly.
- **Identity-aware keys, partial.** The `llm` class keys on Clerk `sub` when a valid bearer token is present, IP otherwise — giving an authenticated caller a higher per-minute allowance (2x anonymous) plus an explicit daily quota, while anonymous traffic keeps the tighter IP bucket with no daily cap. Every other class still keys on IP only; extending identity-aware keying to non-`llm` classes is deliberately deferred (`plan.md` §16.6 Phase 9c), not a regression.
- **Alternatives that existed:** Keep both limiters and add `slowapi` for FastAPI — explicitly rejected in `plan.md` §16.3 for recreating the exact divergent-duplication problem this replaced. App-layer limiting alone, with no edge WAF — rejected because every 429 returned by application code still costs a full metered function invocation; refusing a flood in Python means paying, per request, to say no.
- **Why this won:** This is the fix `plan.md` §16.3-L2 specified in detail before it was built — nothing improvised at implementation time.
- **What would break if removed:** Reopens both the divergent-duplication failure mode and the per-instance-counter defect this replaced.
- **Follow-up questions I should be able to answer:**
  - Is `RATE_LIMIT_REDIS_URL` actually set in production? Yes — operator-confirmed 2026-08-26.
  - Does `/api/stack/health` still describe the old two-limiter shape? No — its `security` block now reads `rate_limit.RATELIMIT_ENABLED`, `rate_limit.RATE_LIMIT_REDIS_URL`, and the live `_POLICIES` table directly off the module, closing the prior "confidently wrong answer" gap.
  - Is the fail-open/fail-closed posture actually tested? Yes, since 2026-08-26.
  - What's still open? Full identity-aware quotas for classes other than `llm` (Phase 9c, deliberately deferred) and the edge WAF layer's own rules, which are a separate concern from this middleware (`plan.md` §16.3-L1).

### Suspicious / thin areas — caching, rate-limiting, cost control

This is the area you already flagged as weakest. Concrete, ordered roughly by exploitability:

1. **The per-user budget check is a check-then-act race, not atomic.** No transaction, row lock, or atomic increment guards it. **Concrete exploit:** an authenticated user at $1.90 of $2.00 fires 20 concurrent `POST /ask` requests (trivial — a few lines of async code, or 20 browser tabs). All 20 read the same pre-request total, all pass, all proceed to call the LLM before any of their spend-recording writes land. At realistic per-call cost, that's a plausible $0.60–$1.60 overshoot from one burst, repeatable immediately.
2. **The check caps cumulative spend, not the cost of a single call** — a caller starting the day at $0 can always get at least one call through regardless of atomicity, since the guard is the running total, not a per-request ceiling.
3. **No global spend ceiling is actually enforced anywhere** — only an opt-in, unconfigured-by-default alert, never a block, checked once a day.
4. **The per-user budget is trivially defeated by creating new accounts.** The ceiling keys on Clerk `user_id`; nothing limits accounts-per-person or links accounts by device/IP/fingerprint for budget purposes, and Clerk signup is frictionless by design.
5. **The IP-fallback key for anonymous callers only matters if `CLERK_ENFORCE_AUTH` is off** — default is on in production, but `.env.example`'s template value ships `false`; if that's copied verbatim into Vercel's env vars, anonymous `/ask` reopens with a budget key shared by every caller behind one NAT.
6. **The Sefaria disk cache almost certainly doesn't function in production.** It writes to a path resolving inside the deployed function bundle; Vercel's Python runtime filesystem is read-only outside `/tmp`. The write is wrapped in a bare `except Exception: pass` — fails **silently**, no log line, nothing surfacing that this tier is a no-op in the one environment that matters. Every cold-start instance effectively has only the empty-on-cold-start in-memory tier between it and a live Sefaria call — a real cache-miss-storm risk on any traffic spike or fresh deploy.
7. **No size bound on the on-disk cache** (moot if #6 holds in production, real for local/self-hosted use) — 27MB/185 files locally already, no eviction beyond passive TTL-staleness.
8. **Resolved 2026-08-22 (`plan.md` §16.9).** This item used to describe two independently-maintained rate limiters with the same intended policy and no shared source of truth — the exact "divergent duplication" failure mode `plan.md` names as the project's most dangerous pattern. Unified into one `backend/rate_limit.py` ASGI middleware; see the rate-limiting section above.
9. **Resolved 2026-08-22, confirmed live in production 2026-08-26.** This item used to describe `memory://` rate-limit storage having no global effect on Vercel (resets per cold start/redeploy, scales with concurrent instance count). `backend/rate_limit.py` is now Redis-backed (`RATE_LIMIT_REDIS_URL`, operator-confirmed set in production), one shared counter across every instance and cold start. The edge WAF (`plan.md` §16.3-L1) and cost circuit breaker (§16.3-L3/§16.10) pieces of the same remediation have also since shipped — see the rate-limiting section above and `plan.md` §16.9/§16.10 for the full trail.

---

# 8. Error handling and observability

## Dual Sentry setup (backend `sentry_sdk` + browser CDN bundle), conditional on env var

- **What was chosen:** Two independent Sentry integrations, both gated purely by env-var presence, both structured so absence means zero SDK activity. Backend: `sentry_sdk.init()` at module import, guarded by a DSN check. Browser: the entire script block (CDN bundle + `sentry-init.js`) is wrapped in a Jinja conditional — when unset, the script tag itself is never emitted, not just an inert init call.
- **Where it lives:** `backend/logging_setup.py:76-119`; `static/js/sentry-init.js`; `templates/index.html:44-61`; `app.py:868-873,1374-1376`.
- **Problem it solves:** Error visibility for a solo-operated app with no on-call, without cost or complexity when Sentry hasn't been set up yet.
- **Alternatives that existed:** A full APM (Datadog/New Relic) — overkill and not free for a two-framework single app. No error tracking at all. Hard-requiring Sentry (crash on missing DSN) — rejected, would break local dev/CI.
- **Why this won:** Free tier plus true-no-op-until-configured means the code ships once and costs nothing until one env var is set — no separate dev/prod code paths.
- **What would break if removed:** No backend exception reaches a human outside manually tailing logs; no browser-side JS error is visible unless a user files a bug report.
- **Follow-up questions I should be able to answer:**
  - Is `SENTRY_DSN` actually set in production? Per `docs/RUNBOOKS.md:35-38` (dated 2026-07-30), it explicitly was not yet configured by the project owner.
  - Why does the browser side get a genuine black-box "never renders when unset" test while the backend side doesn't have an equivalent module-import-level test? See Suspicious.

## `_capture_backend_error` as the single structured-error funnel (log + optional webhook + optional Sentry)

- **What was chosen:** One function is the sole path application code uses to report a handled failure. It always: scrubs the context dict of halachic question/answer text via a sensitive-key substring match, logs a structured JSON line, conditionally POSTs to `ERROR_LOG_WEBHOOK_URL` (2s timeout, exceptions swallowed), and conditionally forwards to Sentry (exceptions swallowed so a Sentry SDK failure never breaks the caller).
- **Where it lives:** `backend/logging_setup.py:274-373`. Called from both `/ask` implementations' outer catches, `routes_devtools.py`'s client-error endpoint.
- **Problem it solves:** Halachic questions are routinely medical/marital/mental-health/abuse-adjacent — a naive direct Sentry call with a raw context dict would leak that text to a third party. One funnel means the redaction logic exists once instead of being reimplemented (and potentially forgotten) at every call site.
- **Alternatives that existed:** Call Sentry directly at each site, relying on `send_default_pii=False` alone — insufficient, that flag only stops automatic PII, not manually-attached context. A logging middleware/decorator instead of an explicit function call.
- **Why this won:** Centralizing scrubbing removes the "did every call site remember to redact" risk entirely; a mirrored substring list exists client-side for defense-in-depth on the browser leg.
- **What would break if removed:** Any route calling Sentry directly would need to reimplement scrubbing per call site, with a high chance one forgets and leaks a real user question.
- **Follow-up questions I should be able to answer:**
  - Which routes do NOT go through this funnel today? Calendar, community, library, and prayers blueprints — see Suspicious.
  - What happens when a plain string (not an exception object) is passed as the error argument? It resolves to `None` for Sentry's purposes — a contexts-only capture, not a properly grouped exception with a stack trace.

## Structured JSON logging with `contextvars`-scoped `request_id`/`user_id`/`client_key`

- **What was chosen:** A custom JSON formatter attached to the root logger; every log line carries `request_id` when bound, plus exception traceback when present. `request_id`/`user_id`/`client_key` are `ContextVar`s rather than thread-locals or explicit parameter-threading, because the app must propagate identity across three execution contexts: WSGI (single call stack), ASGI (`asyncio.create_task`/`to_thread`, which copy context automatically), and a manual `ThreadPoolExecutor` (which does **not** copy context — hence an explicit `submit_with_context()` wrapper).
- **Where it lives:** `backend/logging_setup.py:30-32,122-271`; consumers in `app.py`'s before/after-request hooks, `asgi.py`'s request-ID middleware, `backend/claude.py`'s cost-tracking rows.
- **Problem it solves:** Correlating one user request's log lines across sync Flask code, FastAPI's parallel async tasks, and thread-pool-offloaded work — without this, a slow/failing request produces a scatter of ungroupable log lines.
- **Alternatives that existed:** Explicit parameter threading (verbose, easy to forget at new call sites). A full structured-logging library (structlog). No correlation at all.
- **Why this won:** `contextvars` is stdlib and asyncio already copies it for free; the one real gap (`ThreadPoolExecutor`) is closed with one small wrapper rather than a new dependency.
- **What would break if removed:** Log lines from thread-pool workers and async tasks would silently lose `request_id` — exactly the bug the wrapper exists to prevent.

## Request-ID propagation across the Flask/FastAPI boundary — verified end-to-end, not just per-layer

- **What was chosen:** Two binding points made to agree on one ID per client request. The ASGI middleware runs first (wraps the whole app, including the WSGI-mounted Flask app underneath): it reads `x-request-id` from the incoming request, and if absent, **writes the generated ID back into the raw ASGI header list** before calling onward — so when the request reaches Flask's own `before_request` hook, Flask binds an equal value via the header, not a coincidentally-similar independently-generated one. The ASGI middleware sets the response header last, overwriting anything Flask's own hook already set.
- **Where it lives:** `asgi.py:214-274`, `app.py:1204-1242`.
- **Problem it solves:** Without the header round-trip, a request hitting any WSGI-mounted route would get two different `request_id`s — one bound by each layer independently — making Sentry-issue-to-log correlation impossible for most of the app.
- **Alternatives that existed:** Bind only at the ASGI layer, have Flask read the same contextvar (rejected — contextvar staleness risk on thread reuse, per an explicit code comment). Bind independently at each layer, accept two disagreeing IDs, correlate on timestamp instead.
- **Why this won:** It's testable in-process for the actual "do the two layers agree" question, not just "does each layer produce *an* ID" — a dedicated test captures the Flask-emitted log line and asserts its `request_id` matches the response header the client actually sees.
- **What would break if removed:** Sentry issues and log lines for most of the app (everything except `/ask`) would carry a `request_id` that doesn't match the response header, breaking the "grep logs by the ID in a bug report" workflow the runbook describes.

## Graceful LLM-failure degradation: Gemini → Claude → source-discovery fallback → generic 500 (last resort only)

- **What was chosen:** A four-stage chain, implemented identically on both live `/ask` routes. Both provider call functions catch their own exceptions internally and **always return a dict with an error key, never raise**. If both providers ultimately error, the caller treats that as a `RuntimeError`, caught by an outer handler that runs a non-AI fallback — calling the halakhic-source discovery function, which still returns real source references with a **200** and `fallback: true` in the metadata. Only if that fallback chain also throws does the outer catch return a generic 500.
- **Where it lives:** `backend/claude.py:1690-1924`; `app.py:1877-2019`; `asgi.py:436-726`.
- **Problem it solves:** A total LLM outage does not surface as a raw 500 — the user still gets real source references with clear "AI synthesis unavailable" framing. Client-facing error messages never leak raw provider exception text — a coarse-error-reason mapper (`backend/helpers.py:798-808`) buckets any exception into `blocked`/`timeout`/`rate_limited`/`provider_error` before it reaches the response.
- **Alternatives that existed:** Single-provider with no fallback. Fail fast with a 503 on the primary provider's first error, relying on client retry. Return the raw exception message for debuggability — rejected, exactly what the coarse-reason mapper prevents.
- **Why this won:** For a Q&A app whose entire value proposition is "answer my halacha question," returning real sources without AI synthesis beats returning nothing.
- **What would break if removed:** Any Gemini hiccup would immediately 500 instead of retrying on Claude; removing the discovery-fallback stage would turn any full AI outage into a hard 500 for every request instead of a degraded-but-functional 200.
- **Follow-up questions I should be able to answer:**
  - Is the fallback answer cached? No — deliberately, so the next request can retry.
  - Under what condition is the second provider skipped entirely? When the first error starts with `security_blocked` — a content-policy block isn't retried on a second provider.

## External-API circuit breaker (`backend/health_check.py`) — real for Sefaria/web/translate, dormant for Claude/Gemini

- **What was chosen:** A thread-safe, in-process circuit-state tracker (3-consecutive-failure threshold, 120s recovery window) with two usage modes: active probing for services with a dedicated probe function, and passive tracking for services with no probe, driven entirely by real callers reporting success/failure around actual network calls.
- **Where it lives:** `backend/health_check.py` (full file). Real callers: `backend/utils/search_provider.py` (Sefaria, web search, translation), `backend/routes_calendar.py` (Nominatim geocoding).
- **Problem it solves:** Avoids hammering a known-down external dependency with every incoming request while it's failing — fails fast for 120 seconds instead of paying the full timeout on every call.
- **Alternatives that existed:** A dedicated circuit-breaker library instead of a hand-rolled ~100-line implementation. No circuit breaker at all. A shared/distributed circuit state (Redis) instead of per-process — the rate limiter used to have this exact gap too, and fixed it (`plan.md` §16.9); this module hasn't followed, and isn't written down as needing to elsewhere in this file.
- **Why this won:** Zero new dependencies; per-process state is an acceptable tradeoff for the failure mode it targets, even without a globally-consistent picture across serverless instances.
- **What would break if removed:** Every request touching a down external service would independently pay the full timeout instead of failing fast once the circuit opens.
- **Follow-up questions I should be able to answer:**
  - Are the `claude`/`gemini` circuits actually load-bearing anywhere? No — see Suspicious.

## Health-check endpoints — four different endpoints, four different depths of "healthy"

- **What was chosen:** No single canonical health endpoint. `/api/async/health` is a hardcoded liveness stub — returns 200 constants, doesn't actually verify anything. `/api/stack/health` (auth-gated) reports config-presence booleans plus circuit-breaker state, but constructing a Supabase client object doesn't perform a database round-trip. `/api/devtools/heartbeat` (unauthenticated) is the one functionally real check — it calls a real library/Sefaria-backed function and times it.
- **Where it lives:** `asgi.py:277-284`; `backend/routes_devtools.py:57-93,96-136,253-260`.
- **Problem it solves:** Separates "is the process alive" from "is our configuration present" (auth-gated to avoid reconnaissance) from "can we actually serve a real feature."
- **Why this won:** Practical incrementalism — the heartbeat endpoint backs a real devtools UI feature and grew a functional check as a side effect; the other two are cheap and match their actual callers.
- **Follow-up questions I should be able to answer:**
  - Does any health endpoint catch a broken Supabase connection? No — a valid-looking key pointing at a paused/misconfigured project reports `ready: true`.
  - Does any health endpoint catch a broken Claude/Gemini API key? Only indirectly and passively, and nothing currently feeds real failures into that mechanism — effectively no.

## CI quality gates — what's actually blocking vs. advisory

- **What was chosen:** Five CI steps: ruff lint (`continue-on-error: true` — advisory), pre-commit hooks including gitleaks/bandit (blocking), pip-audit (`continue-on-error: true` — advisory, deliberately until a remediation workflow exists for dev-only CVEs), pytest with coverage (blocking), SonarCloud scan (blocking at the step level; whether it blocks merges depends on branch-protection settings outside this file).
- **Where it lives:** `.github/workflows/ci.yml`.
- **Why this won:** Separates "must never regress" (secrets leaking, tests failing) from "should improve over time but isn't ready to hard-block" (lint debt, unremediated dev-dependency CVEs).

### Suspicious / thin areas — error handling & observability

1. **`/api/stack/health`'s `external_apis` field lies about Claude/Gemini by omission.** Both have real, tested probe functions, but grepping every real caller of the health-check module's report/record functions shows neither is ever checked or updated in the live request path — not even inside `backend/claude.py`'s own call functions, which catch and swallow their own exceptions without ever reporting failure into this circuit breaker. The field will read "up" forever regardless of real provider health, and a dashboard consumer would reasonably assume otherwise.
2. **`/api/async/health` is a hardcoded stub, not a check** — `flask_mounted: true` is a literal constant, never actually verified.
3. **`supabase.ready` in `/api/stack/health` is a config-presence check, not a connectivity check** — the Supabase client doesn't make a network call at construction time; a wrong key or paused project still reports ready.
4. **No global exception handler on either framework — coverage is per-route, and most routes aren't covered.** The structured-error funnel is only called from `/ask`, user routes, privacy routes, and devtools routes. The calendar, community, library, and prayers blueprints all have their own local `except Exception` handling that logs locally but **never reaches Sentry or the webhook** — invisible to any Sentry-based alerting, visible only to whoever is grepping raw stdout logs.
5. **A fully silent exception swallow exists** in the calendar blueprint's parasha lookup — no log line at all, not even at debug level, before falling through to a second fallback.
6. **Bearer-token verification failures are logged at `debug` level, invisible by default** (default `LOG_LEVEL=INFO`) — a wave of expired/malformed/forged tokens hitting `/ask` produces zero log signal; the request just silently falls back to anonymous handling.
7. **No test proves the backend Sentry init is a true no-op at module-import level** — only the browser side has that rigor (a black-box test asserting the script tag never renders when unset). The backend tests monkeypatch the enabled flag directly rather than reloading the module with the DSN unset and asserting `init` was never called.
8. **Per the project's own runbook (dated roughly three weeks before this audit), none of this may actually be live in production** — `SENTRY_DSN`, `DAILY_BUDGET_USD`, and `CRON_SECRET` were all still described as unconfigured operator actions. If still true, today's actual production observability is stdout JSON logs only.
9. **The health-check circuit breaker (`APIHealth`, `backend/utils/search_provider.py`) is still per-process, unenforced across Vercel's concurrent instances** — "circuit open" in one instance has no effect on sibling instances still hammering the same failing dependency. The rate limiter used to share this exact defect; it moved to a Redis-backed shared store in `plan.md` §16.9 (2026-08-22), so this item now applies only to the health-check breaker, not the rate limiter.

---

# Weakest points

Blunt, ranked roughly by how bad it looks if someone pushes hard on it in conversation.

### 1. The entire cost-control system is compromised at its foundation, and the pieces compound each other

The cost-meter's price table doesn't include the actual production Gemini model name (`gemini-3.5-flash-lite` isn't in `backend/cost_meter.py`'s price dict), so most real production LLM calls are logged at **$0.00 cost, silently**. That alone would be bad enough, but it's stacked with: the per-user budget check being a check-then-act race with no atomicity (a burst of concurrent requests can blow past the $2/day ceiling in one shot); the ceiling being trivially defeated by creating a new Clerk account (frictionless signup, no per-person linkage); and the *global* daily-spend alert being opt-in, unconfigured by default, and — even when configured — only an alert, never a block. If asked "what actually stops a bad actor from running up your Anthropic/Google bill today," the honest answer is: not much, and the one number you'd check to notice it happening (`ai_usage_log.cost_usd`) is currently wrong for most rows.

### 2. It's genuinely unclear whether Postgres RLS does anything

Two of five user-data tables are gated by RLS policies that assume `auth.uid()` resolves from a Clerk-issued JWT — which requires Supabase-dashboard configuration (Clerk as a trusted third-party JWT issuer) that exists nowhere in this repository and is unverifiable from code. The team's own code comments (`routes_privacy.py`'s module docstring) admit the two highest-stakes endpoints (account deletion, data export) were deliberately routed around the RLS-scoped client because it "would 403 for exactly the users this feature exists to serve" — which reads as "we tried it, it didn't work for real users, we routed around it" rather than "we verified this works and chose service-role for other reasons." For every other route, RLS is bypassed by design via the service-role key, meaning the *only* thing preventing cross-user data access is a hand-written `.eq("user_id", ...)` filter in application code, with no RLS backstop and no test that would catch that filter being dropped. This needs a direct, verified answer (hit a real endpoint as a real signed-in user and check) before it comes up in conversation, not an assumption either way.

### 3. The retrieval pipeline is keyword matching dressed up with sophisticated-sounding names, and there's dead code duplicating live logic

There is no embedding, no vector search, nothing "semantic" anywhere in this codebase — retrieval is a ~150-entry hand-curated keyword dictionary, substring matching, and hand-tuned point-scoring (`+8 topic hit, +4 source hit...`). That's a legitimate, defensible design choice for a bounded domain, but describing it as anything more sophisticated than "keyword lookup with a good fallback ladder" would not survive a technical question. Separately: `backend/ask_pipeline.py` is a fully-built, professionally-written, test-covered 515-line module that is **not used anywhere in production** — both live `/ask` implementations (Flask sync, FastAPI async) independently reimplement the same orchestration logic instead, and have already begun to drift from each other in small, real ways (retry behavior, prompt-selection thresholds). This already caused one shipped production bug (`ai_cited_sources` silently dropped on one path but not the other). If asked "why do you have three implementations of the same thing, two of them live," the honest answer is "a migration got started, then stalled, and nobody's gone back to finish or delete it."

### 4. Rate limiting — resolved 2026-08-22/2026-08-26, no longer a top weak point

Until 2026-08-22 this section described both rate limiters storing counters in per-process memory, making the real ceiling `(configured limit) × (number of concurrently live instances)` on Vercel rather than the fixed number the config implied — a known, documented, unbuilt-fix situation. That fix has since shipped: `backend/rate_limit.py` is a single Redis-backed ASGI middleware (`plan.md` §16.9), the asymmetric fail-open/fail-closed posture is now directly tested (`tests/test_rate_limit_middleware.py`, 2026-08-26), the edge WAF rules are entered in the Vercel dashboard, and `RATE_LIMIT_REDIS_URL` is operator-confirmed set in the live production environment (2026-08-26, `plan.md` §31.2). If asked "does rate limiting actually work on this platform," the honest answer is now yes, with the residual caveats being ordinary ones (full identity-aware quotas beyond the `llm` class are still Phase 9c, not yet built), not the structural per-instance defect this point used to describe.

### 5. Documentation and "done" status can't be trusted without checking the code directly — and this has already caused a real bug

`docs/DATABASE.md` describes tables, columns, and even a migration workflow (`supabase db push`) that don't exist — it documents a `queries` table that was never built, a `rag_identity_cache` table whose actual name and shape are different, and a primary key column (`clerk_id`) that's wrong everywhere except one place it should have been right. That last one isn't hypothetical: a previous version of the legal-consent-acceptance code used the documented (wrong) column name, PostgREST silently rejected every write, and a broad exception handler swallowed the failure — so consent records were never persisted until someone caught it. Separately, five schema-defining SQL migration files that back currently-used columns are **untracked in git** — if this repo were re-cloned today, there'd be no record they were ever written, let alone applied to production. The pattern across the whole codebase — `plan.md` sections that no longer match the code, health-check fields that report "up" without ever actually checking, a `vercel.json` with region/cron changes not yet committed — is the same failure mode repeated: things that *look* finished, documented, or verified, and aren't. Assume nothing is true until grepped.
