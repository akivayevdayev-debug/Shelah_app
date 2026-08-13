# Sh'elah — Backend Refactor & Optimization Roadmap (`plan.md`)

**Rewritten:** 2026-06-17 · **Graph source:** `graphify-out/graph.json` (built at commit `bc4c32d`, 2026-06-17; 3,718 nodes / 4,636 edges) · **Status:** Planning only — **no code written**. Awaiting explicit command before Phase 1.

---

## 0. Acknowledgement & understanding

I acknowledge the disciplined, sequential execution model: **no code is written across files at once.** Work proceeds in explicit phases (1 → 2 → 3 → 4 → 5), each gated on your command and on the existing `tests/` suite staying green. This document lays out the exact plan and the detailed Phase 1 action list. **I will not output code until you say so.**

### What the graph shows changed since the last roadmap

The repository has already absorbed a large slice of the previous plan. The graph and a file re-scan confirm:

- `app.py` is down from 5,023 → **3,560 lines** but is still the densest node cluster (253 graph nodes; every blueprint references it).
- New modules now exist: `backend/ask_pipeline.py` (432), `backend/auth.py` (117), `backend/cache.py` (93; its `TTLCache` is the #1 god node at **41 edges**), `backend/cost_meter.py` (96), `backend/helpers.py` (861), `backend/rag.py` (376), and six route blueprints (`routes_library/user/calendar/devtools/community/prayers`).
- `tests/` is now a real suite: **22 test modules** including `test_ask.py`, `test_ask_pipeline_smoke.py`, `test_health_check.py`, `test_helpers.py`, `test_cache.py`, `test_cost_meter.py`, route tests, and cache/DST tests. `conftest.py` provides an autouse `mock_outbound_http` fixture — the suite runs offline.
- **No import cycles** exist today (graph: "Import Cycles — None detected"). Preserving that is a hard constraint of this refactor.

### How the core RAG pipeline routes a textual request (verified against the graph)

1. A question enters via either the Flask `/ask` route (in `app.py`) or the FastAPI `/ask` in `asgi.py`. Both delegate the heavy path into `backend/ask_pipeline.py::run_ask_pipeline(...)`, which still receives `app.py` as a `flask_app_module` argument and reaches back into it for `_retrieve_community_knowledge`, `_fetch_user_memory_summaries`, `_knowledge_rows_to_customs`, `RAG_TOP_KNOWLEDGE_ROWS`, etc.
2. Primary retrieval fans out: `flask_app_module.sefaria.find_refs_for_question` → `ShelahEngine.get_library_text` (god node `ShelahEngine`, 26 edges; `get_text` 21 edges) for Sefaria text; in parallel `search.async_search_halachipedia` / `async_search_wikipedia`, plus `_retrieve_community_knowledge` (RAG over Supabase) and user memory.
3. **The keyword/lemmatization and corpus-matching layer that feeds discovery still lives in `app.py`**: `_extract_query_keywords` → `_expand_hebrew_keyword_forms` / `_strip_common_hebrew_prefixes` → `_query_search_wrapper` → `_collect_global_sefaria_sources`, with local-corpus fallback via `_iter_local_json_matches` / `_find_local_custom_matches`, all orchestrated by `get_halakhic_sources(query)` (the fallback entry point invoked when AI synthesis fails).
4. Model synthesis runs through `backend/claude.py` (`ask_ai_async`, 19 edges); the **answer-formatting layer that post-processes model output still lives in `app.py`**: `_normalize_ai_answer` → `_format_ui_answer` → `_bold_halakhic_verdicts` / `_collapse_markdown_spacing`, plus `_strip_model_web_warning_prefix`, governed by the regex constants `CLOCK_TIME_LATEX_RE`, `HALAKHIC_VERDICT_RE`, `HEBREW_DIACRITICS_RE`, and `UI_SECTION_KEYS`.

So the two domains you want extracted — **text/formatting** and **retrieval/corpus matching** — are precisely the two biggest remaining procedural blocks in `app.py`. The target is sound and current.

---

## 1. Target architecture

`app.py` becomes a lightweight traffic cop: Flask init, env bootstrapping, global middleware (`apply_response_cache_policy`, security headers, before/after-request hooks), `_THREAD_POOL` definition, user-scoped Supabase auth helpers, and dynamic blueprint registration. All procedural logic factored into clean, Flask-context-free utility domains:

```
backend/utils/text_engine.py      # typography regex + formatting/normalization (Phase 1)
backend/utils/search_provider.py  # glossary/aliases + lemmatization + retrieval + circuit-broken network (Phase 2-3)
```

Existing `backend/helpers.py` stays, but see the **critical de-duplication finding** below — part of this refactor is resolving the live duplication between `app.py` and `helpers.py`, not adding a third copy.

---

## 2. ⚠️ Critical pre-flight finding — live, divergent duplication (must resolve, "dangerous if ignored")

`backend/helpers.py` was created in the prior refactor and **already contains copies** of several symbols that `app.py` *also still defines and uses*:

| Symbol | In `app.py` | In `backend/helpers.py` | State |
|---|---|---|---|
| `HEBREW_DIACRITICS_RE` | L165 `[֑-ׇ]` | L136 `[֑-ׇ]` (same range, literal) | duplicate |
| `HEBREW_WORD_GLOSSARY` | L174 | L139 | duplicate |
| `_translate_text_google` | L1353 (`requests.get`) | L273 (`_requests.get`, condensed) | **DIVERGED** |
| `_translate_text_mymemory` | L1384 | L301 | **DIVERGED** |
| `_strip_hebrew_diacritics`, `_build_source_attribution_note`, `_sanitize_answer_mode`, `_compact_ai_sources`, `_coerce_int`, `_decode_route_ref` | present | present | duplicate |

`app.py` currently imports only `extract_ai_cited` from `helpers.py` (graph: 3 edges app.py→helpers) — meaning **app.py is running its own local copies, and the helpers.py versions have already drifted** (the translate functions differ in HTTP client handle and echo-check logic). This is the single most dangerous thing in the codebase right now: two implementations of the same function, both live, silently diverging.

**Mandate for every phase below:** when a function targeted for a move already has a twin in `helpers.py`, the step is **reconcile-then-consolidate**, not blind relocation. Procedure: (a) diff the two; (b) determine which behavior is correct (test-anchored); (c) keep one canonical implementation in the new utils module (or re-export from `helpers.py`); (d) delete *both* old copies; (e) point all callers at the canonical one. No phase is complete while two copies of any moved symbol survive.

---

## 3. Existing infrastructure this refactor must reuse (not reinvent)

- **Circuit breaker already exists.** `backend/health_check.py` provides `APIHealth` / `_CircuitState` with `FAIL_THRESHOLD = 3`, `RECOVERY_INTERVAL = 120`, `record_success()`, `record_failure()`, `is_healthy(service)`, and half-open recovery. Phase 3 **wraps `search_provider` network calls with this existing breaker** — it does not introduce a parallel mechanism. (Graph confirms `APIHealth` is already a god-node-adjacent abstraction with test coverage in `test_health_check.py`.)
- **`TTLCache`** (`backend/cache.py`, 41 edges) is the consolidated cache primitive. Any caching introduced in `search_provider.py` uses it, not a new hand-rolled dict.
- **`_THREAD_POOL = ThreadPoolExecutor(max_workers=8)`** (app.py L24) stays in `app.py` and is passed to / imported by utilities that need it. Do not duplicate.
- **Offline test fixtures** (`conftest.py::mock_outbound_http`) already stub Google Translate, MyMemory, Sefaria, Hebcal. New tests for moved functions reuse these.

---

## 4. THE PHASED PLAN

### PHASE 1 — Extract Text & Formatting Operations → `backend/utils/text_engine.py`

**Goal:** move the pure typography/formatting layer out of `app.py` with zero behavioral change and zero Flask-context dependency.

**1.1 Scope (exact symbols, verified present in `app.py`):**

- Regex / constants: `HEBREW_DIACRITICS_RE` (L165), `CLOCK_TIME_LATEX_RE` (L294), `HALAKHIC_VERDICT_RE` (L312), `UI_SECTION_KEYS` (L322). (Note: `HEBREW_DIACRITICS_RE` is a duplication hotspot — see §2; reconcile with `helpers.py` L136.)
- Functions: `_strip_model_web_warning_prefix` (L394), `_normalize_ai_answer` (L471), `_bold_halakhic_verdicts` (L509), `_collapse_markdown_spacing` (L554), `_format_ui_answer` (L586), and their private allies discovered by call-graph closure (e.g. any `_normalize_answer_line`, `_should_drop_debug_line`, `_strip_source_attribution_prefix`, `_compose_answer_with_prefixes` that these call and that are not needed by route logic).

**1.2 Pre-move verification (read-only, before touching code):**
1. Build the precise call-closure of the five target functions using the graph (`graphify path` / node edges) so no transitively-required private helper is left behind in `app.py` (which would create a new `app.py ↔ text_engine` import cycle — forbidden, §0).
2. Confirm none of the targets read Flask globals (`g`, `session`, `request`, `current_app`). Grep each body. If any does, that function is **out of scope** for Phase 1 (it belongs in a route/controller, not a pure util) — flag it, don't move it.
3. Diff `HEBREW_DIACRITICS_RE` against the `helpers.py` twin; they appear equivalent (same Unicode range, one escaped/one literal) — confirm byte-for-byte semantic equivalence with a tiny equivalence check before collapsing to one.

**1.3 Move procedure (when commanded):**
1. Create `backend/utils/__init__.py` (empty) and `backend/utils/text_engine.py` with the module docstring describing the formatting contract and the explicit "no Flask context, pure in → pure out" rule.
2. Copy (not yet delete) the constants + functions verbatim into `text_engine.py`. Fix imports (`re`, typing). Ensure `text_engine` imports **nothing from `app`** and nothing from `helpers` that would loop.
3. Add `from backend.utils.text_engine import (_format_ui_answer, _normalize_ai_answer, _bold_halakhic_verdicts, _collapse_markdown_spacing, _strip_model_web_warning_prefix, ...)` into `app.py`, and leave thin re-export bindings so any blueprint currently importing these names *from `app`* keeps working unchanged (graph shows blueprints reference app.py helpers heavily — `routes_library` 25, `routes_user` 22 incoming edges; backward-compat shims protect them).
4. Delete the original definitions from `app.py` **only after** the import + shim is in place.
5. Resolve the `HEBREW_DIACRITICS_RE` duplication: canonical copy lives in `text_engine.py`; `helpers.py` re-imports it; `app.py` original deleted.

**1.4 Tests & exit criteria:**
- Add `tests/test_text_engine.py`: characterization tests pinning current output of each function on representative inputs (a halakhic verdict line, a clock-time LaTeX artifact, a multi-blank-line markdown block, a web-warning-prefixed answer, a Hebrew-diacritic string). These are written against the **current `app.py` behavior first** (golden master), so the move must reproduce them exactly.
- Existing suite (`test_ask.py`, `test_helpers.py`, route tests) stays green.
- `python -m pytest -q` passes; no new import cycle (re-verify with a quick `python -c "import app, asgi, backend.utils.text_engine"` and a graph re-gen).
- **Defensive-integrity check:** assert truncation/spacing helpers never emit dangling markdown (`**` unclosed) or unclosed HTML — add explicit edge-case tests (string cut mid-`**bold**`, mid-`<tag>`).

**Phase 1 is one reviewable change-set. Stop. Await command for Phase 2.**

---

### PHASE 2 — Extract Retrieval & Corpus Matching → `backend/utils/search_provider.py`

**2.1 Scope (verified present in `app.py`):**
- Linguistic structures: `HEBREW_WORD_GLOSSARY` (L174 — duplicate of helpers L139, reconcile), `HALAKHIC_CORPUS_ALIASES` (L237), `QUERY_STOPWORDS` (L268), `HEBREW_PREFIXES` (L275).
- Lemmatization / expansion: `_strip_common_hebrew_prefixes` (L604), `_expand_hebrew_keyword_forms` (L619), `_extract_query_keywords` (L632).
- Retrieval orchestration: `_query_search_wrapper` (L648), `_collect_global_sefaria_sources` (L808), `_iter_local_json_matches` (L928), `_find_local_custom_matches` (L962), and the pipeline entry point `get_halakhic_sources(query)` (L1115), plus allied private helpers in their call-closure (`_match_corpus`, `_extract_hit_snippet`, `_is_sefaria_hit_relevant`, `_build_discovery_queries`, `_collect_external_global_sources`, `_dedupe_ordered_text`, etc. — exact set determined by graph closure, §2.2-style).

**2.2 Dependency untangling (the hard part):**
- `get_halakhic_sources` and friends call into `ShelahEngine` (data_service), `backend.sefaria`, `backend.sefaria_library` (app.py→sefaria_library is the heaviest outgoing edge, 28), and `backend.search`. `search_provider.py` will import those **backend** modules directly (they don't import `app`, so no cycle). It must **not** import `app`.
- Reconcile the `HEBREW_WORD_GLOSSARY` duplicate (§2) before moving.
- Keep `_THREAD_POOL` in `app.py`; if a moved function uses it, pass it as a parameter or import it lazily inside the function to avoid a structural `search_provider → app` import.

**2.3 Move procedure:** identical discipline to Phase 1 — copy-verbatim → wire imports + back-compat shims in `app.py` (blueprints and `ask_pipeline.py` that reach `flask_app_module.get_halakhic_sources` keep working) → delete originals → reconcile duplicates → update `ask_pipeline.py`'s `flask_app_module.get_halakhic_sources` reference to the new canonical path (or leave the shim; decide by lowest-risk).

**2.4 Tests & exit criteria:**
- `tests/test_search_provider.py`: keyword-extraction cases (Hebrew prefix stripping, stopword removal, glossary expansion), local-JSON-match correctness against a fixture corpus, and `get_halakhic_sources` happy-path + empty-result fallback shape — all offline via existing mocks.
- `test_ask.py` / `test_ask_pipeline_smoke.py` stay green (they exercise `get_halakhic_sources` through the fallback ladder).
- No new import cycle; graph re-gen confirms `search_provider` has no edge to `app`.

**Stop. Await command for Phase 3.**

---

### PHASE 3 — Circuit-breaker hardening on `search_provider` network calls

**3.1 Reuse the existing breaker.** Wrap the external calls now living in `search_provider.py` — Sefaria API requests (via `sefaria_library`), external web/global source collection (`_collect_external_global_sources`), and the translation calls (`_translate_text_google`, `_translate_text_mymemory`, post-reconciliation) — with `backend.health_check.health`:
- Before a call: `if not health.is_healthy('sefaria'): <skip to fallback>`.
- On success: `health.record_success('sefaria')`. On timeout/connection exception: `health.record_failure('sefaria')` then **fail open** to local corpus matching (`_find_local_custom_matches` / `_iter_local_json_matches`) so the user always gets *something*.
- Per-service keys: `'sefaria'`, `'hebcal'`, `'translate_google'`, `'translate_mymemory'`, `'web'` (extend `health_check` service registry if needed — additive, non-breaking).

**3.2 Defensive guarantees:**
- Every wrapped network call gets an explicit timeout (verify `requests`/`httpx` calls pass `timeout=`; several currently rely on session defaults).
- Catch is **narrow** (`requests.RequestException`, `httpx.HTTPError`, `TimeoutError`) — not bare `except Exception`, so logic bugs still surface. Log at `warning` via `logging_setup`.
- Fallback path is total: if *all* external providers are circuit-open, `get_halakhic_sources` still returns a well-formed payload from local corpus, never raising to the route. This is the "zero user-facing downtime" guarantee.

**3.3 Tests:** extend `test_health_check.py` patterns — simulate N consecutive failures, assert circuit opens, assert `get_halakhic_sources` transparently returns local-corpus results while open, assert half-open re-probe after `RECOVERY_INTERVAL`. Use a monkeypatched clock (suite already does this for rate-limit/cache tests).

**Stop. Await command for Phase 4.**

---

### PHASE 4 — Streamline `app.py` & finalize wiring

1. Confirm all migrated blocks (regex, glossary tables, formatting fns, retrieval fns) are **deleted** from `app.py`; only re-export shims (if retained for blueprint back-compat) remain, clearly commented as compatibility shims with a removal ticket.
2. Verify `app.py` retains exactly: Flask app construction, env/bootstrapping, global middleware + security headers, `_THREAD_POOL`, user-scoped Supabase auth helpers (`_get_user_scoped_supabase_client`, `_verify_clerk_token` glue — or confirm these already live in `backend/auth.py`; graph shows `auth.py` exists, 6 edges from app — reconcile any remaining auth duplication the same way as §2), blueprint registration, and the thin `/ask` route that delegates to `ask_pipeline`.
3. Re-point internal callers to canonical import paths where low-risk; keep shims only where a shim removal would touch many blueprint files (defer those to a dedicated cleanup change-set).
4. Final full-suite run + graph regeneration (`graphify update .` per project rules) + `app.py` line-count check (target: meaningfully below 3,560; the formatting+retrieval blocks are ~700–900 lines).

**Stop. Await command for Phase 5.**

---

### PHASE 5 — Advanced Concurrent Fault-Tolerance & Thread Isolation

**Goal:** harden the two remaining concurrency seams in the hybrid WSGI/ASGI runtime — the sync→async bridge in `backend/claude.py` and the unsynchronized module-level caches in `backend/sefaria_library.py` — with zero behavioral change under single-threaded operation.

**5.1 Hybrid event-loop isolation bridge (`backend/claude.py`)**

**Verified current state:** `_call_primary_model_sync` (L1037) calls `asyncio.run(_call_primary_model(...))` directly. Its docstring asserts it is only ever invoked from Flask worker threads (where no loop runs, so `asyncio.run()` is safe today). That assumption is load-bearing and unenforced: if any future caller invokes it from a thread that *does* own a running loop (an ASGI handler, a misplaced `to_thread` unwrap, a test harness), `asyncio.run()` raises `RuntimeError: asyncio.run() cannot be called from a running event loop`; the adjacent failure mode is aiohttp/anyio timeout contexts created against the wrong loop (`Timeout context manager should be used inside a task`).

1. **Guarded bridge:** wrap the `asyncio.run()` call in a loop-state probe — `try: asyncio.get_running_loop()` / `except RuntimeError:` → no loop in this thread → current fast path (`asyncio.run`) unchanged. If a running loop **is** found, submit `asyncio.run(coro)` to a dedicated, module-level, lazily-created single-purpose executor (`ThreadPoolExecutor(max_workers=2, thread_name_prefix="claude-loop-bridge")`) and block on `future.result(timeout=AI_TOTAL_BUDGET_SECONDS)`. The coroutine then executes in a fresh loop in an isolated thread — no nested-loop collision, no cross-loop timeout contexts.
2. **Constraints:** the bridge executor lives in `backend/claude.py` (it must NOT import `app._THREAD_POOL` — `backend/*` never imports `app`, §5). Executor is created once, reused, and never grows unbounded (two workers is the ceiling; the bridge is an escape hatch, not a throughput path). The fresh-loop path must close its loop deterministically (`asyncio.run` already guarantees this — do not hand-roll `new_event_loop`/`run_until_complete`).
3. **Logging:** taking the escape-hatch path logs a `warning` via `logging_setup` (it indicates a caller violating the sync-context assumption — visible, not silent).

**5.1 Tests (`tests/test_loop_bridge.py`):**
- Sync-context call (no running loop) → identical result to today, fast path taken (assert via caplog: no bridge warning).
- Call from inside a running loop (wrap in `asyncio.run(asyncio.to_thread(...))` inverted — i.e., invoke the sync fn from a coroutine's thread via a stub) → completes without `RuntimeError`, bridge warning logged, result correct.
- No leakage: repeated bridge calls reuse the same executor threads (assert thread-name prefix count stays ≤ 2); no stray unclosed event-loop `ResourceWarning` (run with `-W error::ResourceWarning`).
- Timeout propagation: a coroutine exceeding the budget raises through `future.result(timeout=...)` cleanly.

**5.2 Concurrency-safe memory caches (`backend/sefaria_library.py`)**

**Verified current state:** seven module-level dict caches (`_cache`, `_resolved_title_ref_cache`, `_resolved_query_ref_cache`, `_title_catalog_cache`, `_search_query_cache`, `_library_index_adjustments_cache`, `_library_index_view_cache`) are read and mutated with no synchronization. Two distinct race classes exist:
- **Check-then-set windows** (e.g. `_cached_get` L233–253, `_resolve_title_ref` L480–513): benign-looking duplicate work, but under concurrent misses both threads issue the network call and the second write clobbers the first — wasteful, and unsafe if entries are ever partially built.
- **Torn multi-key reads** (`_title_catalog_cache`, `_library_index_view_cache`): `ts`, `report_mtime`, and `data` are written as **separate key assignments** (L649–651), so a concurrent reader can observe a fresh `ts` with stale `data` — a genuine consistency bug, GIL notwithstanding.

1. **Migrate to `TTLCache` where it fits.** Per §3 ("reuse, don't reinvent"), the plain TTL dicts (`_cache`, `_search_query_cache`, `_resolved_*_ref_cache`) migrate to `backend.cache.TTLCache`. First **verify `TTLCache` is itself thread-safe**; if it isn't, add an internal `threading.Lock` there — one fix, 41 edges of benefit. Preserve the disk-cache write-through in `_cached_get` (lock is NOT held during network or disk I/O — see rule 3).
2. **Atomic-swap for multi-key caches.** `_title_catalog_cache` / `_library_index_view_cache` / `_library_index_adjustments_cache` change from mutate-keys-in-place to **build a complete new dict, then a single reference assignment** (`_title_catalog_cache = new_state` via a module-level rebind, or a one-key holder `{"state": ...}`). A single reference swap is atomic in CPython; readers see either the old complete state or the new complete state, never a torn mix. A `threading.Lock` guards only the build-and-swap on the writer side (readers stay lock-free).
3. **Lock discipline:** locks guard *check/mutate boundaries only* — never held across network calls, disk I/O, or JSON parsing. On a miss: release the lock, do the fetch, re-acquire, re-check (another thread may have won), then write.
4. **Immutability instead of deepcopy.** Cached values are treated as **immutable after insertion** — callers must not mutate returned structures (audit callers; where a caller does mutate, that caller takes a shallow/targeted copy at its own boundary). Blanket `copy.deepcopy` inside critical sections is explicitly **rejected**: deep-copying Sefaria catalog/text payloads per hit while holding a lock would serialize all readers behind O(payload) copies and multiply latency on the hottest read path. (Deviation from the original spec — recorded deliberately.)

**5.2 Tests (`tests/test_sefaria_library_concurrency.py`):**
- Hammer test: N threads (≥16) concurrently hitting `_cached_get`/`resolve` paths on overlapping keys against mocked HTTP (existing `mock_outbound_http`) — no exceptions, exactly-consistent final cache state, and (with a call-counting mock) at-most-bounded duplicate fetches.
- Torn-read regression: reader thread loops on `_title_catalog_cache` consistency invariant (`ts` fresh ⇒ `data` matches) while a writer swaps repeatedly — invariant never violated.
- Immutability guard: mutate a returned catalog row in a test → assert the cache's copy is unaffected (or the caller-boundary copy exists).
- Lock-scope check: assert (via a patched slow network mock + timing) that a cache **hit** never blocks behind another thread's in-flight miss.

**5.3 Exit criteria:** full suite green; no new import cycle (`backend/claude.py` and `sefaria_library.py` still import zero from `app`); `graphify update .`; behavior byte-identical in the single-threaded test paths (golden masters from Phases 1–2 still pass).

**Stop. Await command. This completes the backend phased plan. (Phase 6 — product surface & growth — lives in §12; it is frontend/content work, deliberately kept out of the backend sequence.)**

---

## 5. Strict constraints & behavioral guarantees (carried in every phase)

- **No signature breaks.** Blueprint contract names, payload types (`AskRequest`, route JSON shapes), and external endpoints unchanged. Back-compat shims protect the ~90 incoming blueprint→app references the graph records.
- **Defensive integrity.** Truncation/formatting helpers keep exact behavior: no dangling markdown (`**`, `__`, backticks) and no unclosed HTML after any split/cap. Explicit edge-case tests enforce this (§1.4).
- **Test alignment.** `tests/` must stay green at every phase boundary. New modules get new test files mirroring the existing naming convention; characterization (golden-master) tests are written *before* each move so behavior is pinned to current output.
- **No import cycles.** `text_engine.py` and `search_provider.py` import only stdlib + `backend.*` leaf modules, never `app`. Re-verified by graph after each phase.
- **Reuse, don't reinvent.** Existing `TTLCache`, `APIHealth` circuit breaker, `_THREAD_POOL`, and offline test fixtures are reused.
- **De-dup is mandatory.** The §2 divergent duplicates are reconciled to a single canonical implementation as part of whichever phase touches them; the refactor does not finish with two live copies of any symbol.

---

## 6. Phase-gate checklist (per phase)

1. Read-only graph closure of target symbols → no orphaned private helper.
2. Golden-master tests written against current behavior.
3. Copy → wire imports + shims → delete originals → reconcile duplicates.
4. `pytest -q` green; `python -c "import app, asgi, <new module>"` clean.
5. Graph regenerate; confirm zero new cycles and reduced `app.py` density.
6. **Stop and report; await command for next phase.**

---

## 7. Deferred (previously-documented) roadmap — frontend & platform

The following items from the prior roadmap remain valid and are **not** part of the backend refactor above. They are parked here, unchanged in intent, to run *after* the four-phase backend work (or independently, since they touch disjoint files). Summarized to keep this document focused:

- **AI source box fix** — `innerHTML +=` in render loops destroys earlier click handlers; inconsistent `escapeHtml`; duplicate `.ai-source-card` styles; capped nth-child stagger. → single-insert + delegated listeners + per-box `--i` stagger + skeletons.
- **Observability** — structured JSON logging with request IDs across threads/asyncio, Sentry via `_capture_backend_error`, `cost_meter.py` token/USD logging to Supabase (note: `cost_meter.py` now exists — verify it covers all model call sites).
- **Docs pass** — README, `SERVICE_ARCHITECTURE.md`, new `API.md`/`OBSERVABILITY.md`/`FRONTEND.md`, env-var table.
- **Motion overhaul** — vanilla `motion` (motion.dev) on current surfaces, Framer Motion on any future React islands, via the `ui-ux-pro-max` tool workflow; zero new `@keyframes` outside the token layer (rules in `.agents/ENGINEERING_RULES.md`).
- **Dark mode overhaul** — collapse the ~570 scattered `body.theme-dark` overrides into a semantic token layer with `prefers-color-scheme` + no-FOUC head script; AA-contrast audit both themes; 21st.dev component patterns as QA baseline.
- **Loading-states overhaul (light + dark)** — token-driven loading design system; AI loading animation rebuilt off hardcoded hex with dark variants + timer cleanup; skeletons for all "Loading..." text regions; `role="status"`/`aria-live`; reduced-motion guards.
- **Frontend platform fixes** — replace the production Tailwind CDN JIT compiler with a build step (kills the CSP `'unsafe-eval'`), SRI/version-pin CDN scripts, font preconnect + woff2, SEO/OG metadata, dynamic `lang`/`dir` for Hebrew.

Full detail for these lives in version history; the backend four-phase plan above is the active, commanded work.

---

## 8. Production-readiness program — everything required to launch safely

> **Not legal advice.** I am not a lawyer and this is not legal advice. Sh'elah gives religious/halachic guidance via AI and processes personal data across jurisdictions — a combination with real liability exposure. Every legal document and disclosure below is **draft-level scaffolding to hand to a licensed attorney** (ideally one familiar with software, AI, and consumer-privacy law in your operating jurisdiction). Treat attorney review as a hard launch gate, not optional polish. The engineering items are mine to implement; the legal items I can draft, but a human lawyer signs off.

This program runs in parallel with (and after) the backend refactor. It is grouped into workstreams A–H with concrete deliverables. Each item is a checklist line so it can be tracked.

### A. Legal documents & disclosures (attorney-review gated)

Current state (scanned): `terms.html` exists with 11 sections **but is missing §6, §10, §12** (numbering jumps — likely deleted content), and `privacy.html` has 9 sections including a GDPR/CCPA rights stub. Both need substantial expansion. Deliverables:

1. **Terms of Service — rewrite & complete.** Fill the missing sections and add: definitions; eligibility/age (18+, or 13+ with consent — see child-safety, §B); account terms (Clerk auth); acceptable-use policy; **AI-output disclaimer** (outputs are machine-generated, may be wrong/incomplete, are *not* a halachic ruling — see §C); **"not religious, legal, medical, or financial advice"** clause; user-content license (questions/bookmarks they submit); intellectual-property & third-party-content attribution (Sefaria, Hebcal, Wikipedia, Halachipedia — confirm each license permits your use and display); indemnification by user; **limitation of liability + damages cap + "AS IS"/"AS AVAILABLE" warranty disclaimer** (the core anti-lawsuit clauses); assumption of risk for reliance on religious guidance; dispute resolution (governing law, venue, and a decision on **binding arbitration + class-action waiver** — discuss enforceability with counsel for your jurisdiction); DMCA/takedown process and agent contact; termination; changes-to-terms mechanism; severability; entire-agreement; contact. Add a **"Last updated" date and version**, and require **click-through acceptance** (you already have `/api/accept-legal` + an `accept_legal` route — wire ToS+Privacy versioned consent through it and store consent timestamp + version per user).
2. **Privacy Policy — expand to a complete notice.** Sections to add/strengthen: exhaustive **data inventory** (what's actually collected — from the scan: Clerk identity/auth tokens, Supabase-stored bookmarks/preferences/user-memory summaries of questions, IP for rate-limiting, error logs, AI usage/cost logs); **purposes & legal bases** (GDPR Art. 6 — consent/legitimate interest per purpose); **processors/sub-processors list** with links (Anthropic, Google/Gemini, Clerk, Supabase, Vercel, Hebcal, Sefaria, MyMemory/Google Translate, Sentry) and what each receives; **international transfer** mechanism (US processing → SCCs/DPF for EU users); **retention schedule** per data type; **user rights** (access, deletion, portability, rectification, objection, withdraw consent) **with a working mechanism**, not just prose (see §D — data-subject-request flow); **"Do Not Sell/Share"** + Global Privacy Control honoring for CCPA/CPRA; **children's privacy** (COPPA — if under-13 are ever possible, you need verifiable parental consent or a hard 13+ gate); cookies/local-storage disclosure (you use `localStorage` for theme/consent — disclose it); security overview; breach-notification commitment; contact + (if EU) an EU representative and a DPO determination.
3. **AI Transparency / Disclosure Statement (new doc).** A plain-language page: this product uses LLMs (Anthropic Claude, Google Gemini); how RAG works at a high level; that sources are retrieved from third-party corpora and may be mis-matched; that the system can "hallucinate"; what it does *not* do (issue binding p'sak halacha, replace a rabbi/posek, give legal/medical advice); the human-in-the-loop expectation ("verify with your rabbi"). The EU AI Act requires disclosure that users are interacting with an AI system — this satisfies it and is good practice everywhere.
4. **Cookie / local-storage notice + consent** (if you add any analytics or non-essential cookies later; today it's first-party essential only — document that, and gate any future analytics behind consent for EU).
5. **Acceptable Use Policy** (can live in ToS or standalone): no scraping, no attempting to extract another user's data, no using outputs to harass or to represent the app's answers as authoritative rulings, no reverse-engineering, rate-limit respect.
6. **DMCA / Copyright policy** with a designated agent (register with the U.S. Copyright Office if you want safe-harbor protection) — relevant because you display third-party texts.
7. **Accessibility statement** (ties to the WCAG work in `ENGINEERING_RULES.md`) — increasingly expected, and ADA web-accessibility suits are a real litigation vector in the US.
8. **Licensing/attribution page** — explicit credit and license terms for Sefaria (much is CC-BY or public domain, but **verify per-text**, some is restricted), Hebcal, Wikipedia (CC-BY-SA — attribution + share-alike implications), fonts (`SILEOT.woff` — confirm its license permits web embedding/redistribution), DaisyUI/Tailwind/marked/DOMPurify (MIT — include notices). A `NOTICES`/`THIRD_PARTY_LICENSES.md` file in-repo plus a UI credits page.

### B. AI-specific liability & religious-guidance safety (the highest-risk area)

This is where a "Torah encyclopedia / halachic AI" is most exposed. Deliverables:

1. **Persistent, unavoidable disclaimer in the answer UI** (not just buried in ToS): every AI answer renders a visible "Educational information, not a halachic ruling — consult your rabbi" banner. The backend already appends a `RABBI_FINAL_RULING_FOOTER` ("Please consult with your local Rabbi for a final ruling.") — surface it prominently and make it non-dismissible-by-default on first use.
2. **Scope guardrails** (partly built): the out-of-scope/prohibited detection in `claude.py` (`_detect_out_of_scope_subject`, `is_prohibited`) should hard-refuse and redirect for anything touching medical, legal, mental-health, or dangerous-practice questions dressed as halacha (e.g., fasting with a medical condition, mikveh safety, circumcision/medical, abuse situations). Add an explicit **safety routing layer**: medical/self-harm/abuse-adjacent queries return a referral to professionals + relevant resources, never a halachic "ruling."
3. **No-impersonation rule:** the model must never claim to *be* a rabbi or to issue p'sak. Audit system prompts for first-person rabbinic authority phrasing.
4. **Sensitive-topic handling:** content involving minors, abuse, self-harm, or medical emergencies follows a documented escalation/referral pattern, never procedural instruction.
5. **Provenance on every claim:** keep tightening source attribution so users can verify; "internal AI knowledge" answers are clearly labeled as lower-confidence (already partly done via `_build_source_attribution_note`).
6. **Logging for defensibility:** retain (within privacy limits) enough request/response/version metadata to reconstruct what the system actually said if a dispute arises — paired with the retention schedule in §A.2.

### B-AGE. Minimum age = 13+ and age-appropriate AI output (decision + implementation)

**Decision (recommended): set the minimum age to 13+.** Rationale: 13 is the lowest floor that avoids COPPA's verifiable-parental-consent burden (which attaches to under-13 in the US), while keeping the natural student/bar-bat-mitzvah audience. Conditions that travel with this choice:

- **EU/UK:** GDPR digital-consent age is 13–16 by member state. Either gate EU users to **16+** or obtain parental consent for the 13–15 band (counsel decides; default EU to 16+ if unsure). Reflect the chosen rule in the age gate and Privacy Policy.
- **No behavioral advertising and no sale/share of personal data** for under-18 users (already the case — state it explicitly; honor GPC).
- **COPPA hygiene:** no *actual knowledge* of under-13 users — the gate must be a real barrier, and any self-identified under-13 account is refused/closed.
- **Age gate mechanism:** a neutral age-collection step at sign-up (date-of-birth or 13+/16+ attestation tied to Clerk), stored with the consent record (§D.2); ToS eligibility clause (§A.1) states the minimum; under-age attempts are blocked, not nudged.

**Because the corpus is halachic, age-appropriateness is required even at 13+.** The following AI-prompt optimization makes every response suitable for a 13+ reader without crippling scholarly depth. It is grounded in the current `backend/claude.py` prompt architecture (`CORE_SYSTEM_PROMPT`, `SIMPLE_SYSTEM_PROMPT`, `_build_dynamic_system_context`, structured-JSON output, `_detect_out_of_scope_subject`, `is_prohibited`).

**Implementation — Age-Appropriate Output layer (new):**

1. **Add an `AGE_APPROPRIATE_DIRECTIVE` block** appended to both `CORE_SYSTEM_PROMPT` and `SIMPLE_SYSTEM_PROMPT` (single source of truth, hoisted constant). Content rules the model must follow:
   - Assume the reader may be as young as 13. Use clinical, respectful, educational language for sensitive halachic areas (family purity/niddah, mikveh, intimacy, marital relations, bodily functions). Explain *that* a topic exists and its halachic framework and sources; **never** provide sexually explicit, graphic, or titillating detail, technique, or anatomical description beyond what is strictly necessary to convey the halacha at an educational level.
   - No profanity, no violence-as-detail, no graphic descriptions of harm.
   - When a sensitive topic is genuinely intimate (e.g., hilchot niddah specifics), give the halachic principles and source citations and **explicitly direct the reader to learn the practical details with a rabbi, teacher, or parent**, rather than rendering them inline.
   - Maintain scholarly depth (multiple authorities, sources, machloket) — age-appropriateness constrains *explicitness and tone*, not rigor or honesty.
2. **Safety routing (extend `_detect_out_of_scope_subject`)** into a dedicated `classify_safety(query)` step that runs before synthesis and assigns one of: `ok`, `sensitive_intimate`, `medical`, `mental_health_or_self_harm`, `abuse_or_minor_safety`, `dangerous_or_illegal`.
   - `medical` / `mental_health_or_self_harm` / `abuse_or_minor_safety`: do **not** issue a halachic "ruling"; return a brief, kind response that gives general halachic framing only where safe and **refers to a qualified professional / trusted adult / appropriate hotline**, with the rabbi-consultation footer. (e.g., fasting with a medical condition → "speak with your doctor and rabbi"; self-harm ideation → supportive referral, never method or procedure.)
   - `sensitive_intimate`: allow, but force the `AGE_APPROPRIATE_DIRECTIVE` "principles + refer to rabbi/parent for practical detail" mode.
   - `dangerous_or_illegal`: existing refusal path.
3. **Wire through the existing structured output:** add a returned field (e.g. `age_safe: true|false` and `safety_class`) to the JSON contract so the route/UI can render the right disclaimer and, if needed, swap to the referral template. Keep backward compatibility (default `age_safe: true`, `safety_class: "ok"` when absent).
4. **Two-layer enforcement (defense in depth):** the prompt directive is layer one; add a lightweight **post-generation output check** (extends `validate_model_output`) that scans for explicit-content markers and, on a hit, replaces the body with the principles-plus-referral template rather than shipping it. Never rely on the prompt alone.
5. **Mode interaction:** the directive applies across all answer modes (`balanced`, `practical`, `sources`, `strict`); `practical` steps for intimate topics yield "learn the practical details with your rabbi/teacher" rather than explicit instructions.
6. **Bilingual parity:** the directive and referral templates exist in both English and Hebrew (`answer_language`), matching the existing localized rendering in `render_structured_markdown`.
7. **UI:** pair with the persistent answer disclaimer (§B.1); for `safety_class != ok`, render the referral/resource variant prominently.

**Tests (extend `tests/`):**
- `tests/test_safety_classifier.py`: each `safety_class` routes correctly; medical/self-harm/abuse queries never return a "ruling" and always include a referral; `dangerous_or_illegal` refuses.
- Age-appropriateness fixtures: intimate-topic queries return principles + source citations + "consult rabbi/parent," and the post-generation check strips/repaths any explicit content.
- Regression: ordinary halachic questions (Shabbat, kashrut, brachot) are unchanged and retain full scholarly depth (no over-refusal).
- Bilingual: same behavior in `he`.

**Documentation:** record the age policy and the age-appropriateness/safety-routing design in `docs/` (`AGE_AND_SAFETY_POLICY.md`), reference it from the AI Disclosure doc (§A.3) and Privacy Policy (children's section, §A.2), and add the standing rule ("all AI output must be suitable for 13+; sensitive halacha = principles + referral, never explicit") to `.agents/ENGINEERING_RULES.md`.

### C. Security hardening (pre-launch gate)

1. **Secrets & key management.** Scan confirms many secrets via env (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `FLASK_SECRET_KEY`, Clerk, Supabase). Verify: no secrets committed (`git log`/`git secrets` scan, check `.env` never tracked — `.gitignore` covers it, confirm history is clean; if any key ever touched a commit, **rotate it**); `FLASK_SECRET_KEY` is strong/random in prod; least-privilege Supabase keys (anon vs service-role separation — never ship service-role to the client).
2. **Supabase Row-Level Security.** You have `scripts/sql/SUPABASE_RLS_POLICIES.sql` and a `STRICT_SUPABASE_RLS` flag and `/api/devtools/rls-audit`. Make strict RLS the **enforced default in prod**, run the audit in CI, and verify every user-data table (bookmarks, preferences, memory summaries) denies cross-user access. This is both a security and a privacy-law requirement.
3. **CSP hardening** (already in roadmap §7.10): remove `'unsafe-eval'` (kill the Tailwind CDN compiler), move to nonce-based `script-src`, add `Strict-Transport-Security` (HSTS), `Permissions-Policy`; drop deprecated `X-XSS-Protection`.
4. **Dependency & supply-chain:** SRI/pin all CDN scripts (§7.10); `pip-audit`/Dependabot on `requirements.txt`; SCA in CI; the `.pre-commit-config.yaml` already present — extend it with secret-scanning (`detect-secrets`/`gitleaks`).
5. **Input validation & abuse:** prompt-injection defenses exist in `claude.py` — keep as defense-in-depth; ensure all route inputs are validated (coordinate bounds, ref decoding, payload caps already partially present); confirm the rate limiters (Flask-Limiter + ASGI deque) are active in prod and consider a shared store (§ earlier roadmap) since per-instance limits are weak on serverless.
6. **AuthN/Z review:** Clerk JWT verification path (`_verify_clerk_token`), `CLERK_ENFORCE_AUTH` on for protected routes in prod, token-in-cookie handling reviewed, session cookie flags (`Secure`, `HttpOnly`, `SameSite`) verified in `apply_session_cookie_policy`.
7. **Penetration test / security review** before launch (the `security-review` skill or an external pass); document findings + remediation.

### D. Privacy operations (makes the policy real, not just words)

1. **Data-subject request (DSR) flow:** a working path for access/export/delete — at minimum an email intake + documented internal procedure; ideally a self-serve "download my data" and "delete my account + data" in settings (deletion must cascade through Supabase tables and Clerk).
2. **Consent records:** store ToS/Privacy version + timestamp at acceptance (wire through existing `/api/accept-legal`); re-prompt on material version changes.
3. **Data Processing Agreements (DPAs):** execute/accept DPAs with each processor (Anthropic, Google, Clerk, Supabase, Vercel, Sentry) — most offer click-through DPAs; keep copies.
4. **Records of Processing (GDPR Art. 30)** and a **basic DPIA** given you process data to drive automated religious guidance.
5. **Retention enforcement:** scheduled job actually deleting data past its retention window (not just a policy promise).
6. **Breach response plan:** who, what timeline (GDPR 72h), notification templates.

### E. Reliability, observability & operations

1. **Observability finished** (roadmap §7.3 / partly built — `SENTRY_DSN`, `cost_meter.py`, `logging_setup.py` exist): confirm Sentry initialized in prod, structured request-ID logging across Flask + asyncio, cost metering on every model call with a budget alert, and the circuit breakers (§Phase 3) wired on all external calls.
2. **Health checks & uptime monitoring:** `/api/health` + `/api/stack/health` exist — wire an external uptime monitor + alerting.
3. **Error budgets & graceful degradation:** verify the fail-open fallback (local corpus) path end-to-end; user-facing error states are friendly and never leak stack traces (CSP/headers + generic 500s).
4. **Backups & recovery:** Supabase backup cadence confirmed; documented restore procedure; export of customs/config data.
5. **Runbooks:** incident response (the `engineering:incident-response` skill), on-call expectations even if just you, rollback procedure (Vercel preview → promote), and the deploy checklist (`engineering:deploy-checklist`).
6. **Load/cost ceiling:** rate limits + a hard monthly spend cap on the AI providers so a traffic spike or abuse can't produce a surprise bill.

### F. Quality, accessibility & content integrity

1. **Test coverage gate** (roadmap §7.2 — suite now exists): enforce coverage in CI; add the golden-master tests from the refactor phases.
2. **Accessibility to WCAG 2.1 AA** (per `ENGINEERING_RULES.md`): automated axe-core/pa11y in CI + manual audit; publish the accessibility statement (§A.7).
3. **Content QA for religious accuracy:** a documented review process / disclaimer acknowledging the app is not rabbinically supervised (or, if you obtain a rabbinic advisor/hechsher-equivalent endorsement, document its scope). State clearly which it is — overclaiming authority is itself a liability.
4. **Localization correctness:** Hebrew/RTL correctness (dynamic `lang`/`dir`, §7.10) and that translated content isn't presented as authoritative.

### G. Business & compliance scaffolding (discuss with attorney/accountant)

1. **Entity & insurance:** operating through an LLC/corp (liability shield) and **tech E&O / general liability / cyber insurance** are the practical backstops behind the contractual disclaimers — arguably the single most effective "don't get sued into personal bankruptcy" step. Discuss with counsel.
2. **Trademark/name clearance** for "Sh'elah" and logo; domain/brand.
3. **Accessibility/consumer-protection posture** for your launch markets (US ADA, EU EAA 2025, etc.).
4. **AI-specific regimes:** EU AI Act transparency obligations (§A.3); any future US state AI disclosure laws — monitor.
5. **Payment/commerce:** if you ever monetize, that triggers a separate stack (refund policy, billing terms, PCI via the processor, tax) — out of scope until then, but flag it.

### H. Launch gate — definition of "production ready"

A single checklist that must be fully green before public launch:

- [ ] ToS, Privacy Policy, AI Disclosure, AUP, DMCA, Accessibility, Licenses — drafted **and attorney-reviewed**, versioned, dated, click-through consent wired and stored.
- [ ] Persistent AI answer disclaimer live; safety routing for medical/legal/abuse queries verified.
- [ ] Age gate (13+, EU 16+ or parental consent) enforced at sign-up + stored with consent; AGE_APPROPRIATE_DIRECTIVE + safety classifier + post-generation output check live and tested in en/he.
- [ ] Security: secrets clean + rotated if needed, strict RLS enforced + audited, CSP hardened, SRI/pinned deps, secret-scanning in CI, auth review done, pen-test/security-review complete.
- [ ] Privacy ops: DSR + account-deletion flow working, DPAs executed, retention job running, breach plan documented.
- [ ] Observability: Sentry + structured logs + cost metering + budget cap + uptime alerts live; circuit breakers wired; fallback path verified.
- [ ] Quality: test coverage gate passing, WCAG AA audited, accessibility statement published.
- [ ] Business: entity + insurance in place, trademark cleared.
- [ ] Backups + restore tested; rollback runbook validated on a preview deploy.

### Where this is written down
- Legal documents live in `templates/` (`terms.html`, `privacy.html`, new `ai-disclosure.html`, `acceptable-use.html`, `dmca.html`, `accessibility.html`, `licenses.html`) — rendered via the existing legal-page pattern (`components/legal_topbar.html`, `legal_scripts.html`), bilingual (en/he) like the current ones, versioned and dated.
- Operational/compliance docs live in `docs/` (`SECURITY.md`, `PRIVACY_OPERATIONS.md`, `DPIA.md`, `RUNBOOKS.md`, `LAUNCH_CHECKLIST.md`, `THIRD_PARTY_LICENSES.md`).
- Engineering enforcement (CSP, RLS-in-CI, coverage/a11y gates, secret-scanning) is added to `.pre-commit-config.yaml` and a new CI workflow, and the standing rules go into `.agents/ENGINEERING_RULES.md`.

---

## 9. Agentic tool-use layer for the halachic AI (new capability)

**Current state (scanned):** the AI is *not* agentic today. `app.py` / `asgi.py` pre-fetch everything (Sefaria refs via `sefaria.find_refs_for_question`, customs, wiki, halachipedia) and hand it to `claude.ask_ai_async(..., tool_context=...)` as static `extra_context`. The model can only use what was pre-stuffed; it cannot decide "I need today's zmanim" or "this isn't in the texts, search the web." This section adds genuine tool-calling (Anthropic tool use / function calling) so the model can pull live Jewish-calendar/zmanim data and fall back to web search **only as a last resort**.

### 9.1 Design principles

1. **Texts first, web last.** Tool selection must honor the existing source hierarchy (`CORE_SYSTEM_PROMPT` "Source priority"). Judaic-text and computed-calendar tools are always preferred; `web_search` is explicitly the **last-resort** tool, callable only when the Judaic corpus + calendar tools cannot answer.
2. **Deterministic data stays deterministic.** Zmanim, Hebrew-date conversion, parasha, omer, and holidays come from the existing **computation engines** (`zmanim_engine.py`, `calendar_service.py` / `PyluachEngine`, Hebcal), never hallucinated. The model calls a tool; the tool returns ground truth; the model explains it.
3. **Every tool call is logged, costed, and circuit-broken** — reuse `cost_meter.py`, `logging_setup` request IDs, and the `health_check` circuit breaker (Phase 3). A tool whose provider is circuit-open is hidden from the model that turn.
4. **Bounded agency.** Hard cap on tool-call rounds per question (e.g. 4) and per-tool timeouts, so a question can't loop or run up cost.

### 9.2 Tool catalog (wrapping existing backend functions — no new logic, just exposure)

Define a tool registry `backend/ai_tools.py` (pure functions + JSON schemas; imports only `backend.*`, never `app`):

| Tool name | Backs onto | Purpose |
|---|---|---|
| `search_judaic_texts` | `sefaria_library` search + `sefaria.find_refs_for_question` | Primary: find/return Sefaria primary sources & commentaries for a query. |
| `get_text_by_ref` | `ShelahEngine.get_library_text` / `sefaria_library.get_text` | Fetch the full Hebrew+English text of a specific ref the model cites. |
| `search_responsa_external` | `search.async_search_halachipedia` (+ HebrewBooks) | Whitelisted external halachic sources (tier 2). |
| `get_zmanim` | `zmanim_engine.get_community_zmanim(lat, lon, tz, community)` | Full halachic times for a date/location/community (candle-lighting, sof zman, plag, tzeit, etc.). |
| `get_hebrew_date` | `calendar_service.PyluachEngine` (+ Hebcal convert) | Gregorian↔Hebrew date conversion, day-of-week, special-day flags. |
| `get_parasha` | `calendar_service.get_parasha` / `zmanim_engine._get_weekly_shabbat_parasha` | Weekly Torah portion (and triennial if relevant). |
| `get_omer` | `zmanim_engine._get_omer_info` | Sefirat HaOmer day/week count for a date. |
| `get_holidays` | `zmanim_engine.get_monthly_events` / Hebcal holidays API | Yom tov / fast / rosh chodesh in a date range. |
| `get_daily_study` | `search` daily-learning (Hebcal/Sefaria calendars) | Daf yomi, mishnah yomi, etc. |
| `web_search` | **last-resort only** — `search.async_search_wikipedia` + the existing whitelisted/allowlisted source set (NO new general web provider) | General-knowledge facts not in Judaic texts/calendar (e.g. real-world context, definitions). Gated by §9.3. |

**Web search scope (locked):** `web_search` uses **only** the existing Wikipedia connector (`async_search_wikipedia`) plus the already-whitelisted/allowlisted domains in `search.py` (`_looks_like_trusted_web_match`, `_build_last_resort_web_sources`). No new general-purpose search API is introduced. This keeps the cost and compliance footprint unchanged from today.

### 9.2b Additional tools (also wrapping existing backend functions)

These expose more of the app's existing capability to the model so answers are richer and more actionable. All back onto code that already exists; none add new external dependencies.

| Tool name | Backs onto | Purpose |
|---|---|---|
| `lookup_word_meaning` | `_lookup_hebrew_word_meaning` / `_lookup_english_word_meaning` (Sefaria lexicon, BDB, Jastrow) | Define/translate a specific Hebrew or English term the user or a source uses — precise lexicon meaning, not a guess. |
| `translate_text` | `_translate_hebrew_text_online` (Google/MyMemory fallback) | On-demand Hebrew↔English translation of a phrase/passage when source text lacks a translation. |
| `get_commentaries` | `sefaria_library.get_linked_texts(ref)` | Pull linked commentaries and cross-references for a ref (Rashi, Tosafot, Mishnah Berurah, etc.) so the model can deepen or contrast a ruling. |
| `search_community_customs` | `customs.search_customs` + `_retrieve_community_knowledge` / `_find_local_custom_matches` | Retrieve minhag for a specific community (Ashkenaz, Sefardi, Teiman, etc.) from the local customs corpus + RAG knowledge. |
| `get_community_profile` | community routes data (`/api/community/<name>`, timeline) | Background on a community's halachic tradition and history for "lens" answers. |
| `browse_library` | `sefaria_library.get_library_index` / `get_texts_for_category` | Navigate the Sefaria category tree — find what texts exist in a topic area before fetching. |
| `search_library` | `sefaria_library.search_library(query, filters)` | Full-text Sefaria search with metadata filters (corpus/category), beyond curated ref mapping. |
| `get_prayer_text` | `routes_prayers` (`get_prayer`, `get_siddur_full`) / `_get_prayer_refs` | Retrieve liturgy — a specific prayer or full siddur service text — for prayer/nusach questions. |
| `get_daily_zmanim_summary` | `zmanim_engine` (composed) | One-shot "today at this location" digest: candle-lighting, key sof-zman times, parasha, omer, holiday flags — for "what do I need to know today" questions. |
| `convert_measurements` | new small util (shiurim) | Convert halachic measures (kezayit, revi'it, amah, tefach, mil, kav) to metric/imperial with the major shitot (Chazon Ish vs. R' Chaim Naeh) — a frequent practical need; deterministic table, not model math. |
| `calculate_hebrew_date_math` | `PyluachEngine` | Date arithmetic on the Hebrew calendar — yahrzeit dates, "what Hebrew date is N days from X," upcoming-occurrence of a Hebrew date (birthday/yahrzeit in coming years). |
| `format_source_citation` | `text_engine` (Phase 1) helpers | Normalize a ref into the house citation format so cited sources render consistently. |

> Optional, behind explicit user action only (not autonomous): `export_answer` → `routes_library`/`/api/export/chapter` (docx/pdf) and `save_bookmark` → semantic-bookmark route. These mutate state or produce files, so they are surfaced as UI actions on a finished answer, **not** as model-invokable tools, to keep the agent loop read-only and side-effect-free.

All tool I/O is JSON-schema'd (name, description emphasizing *when* to use it, typed params with bounds — lat/lon ranges, date formats, community enum). Descriptions encode the hierarchy ("Use `search_judaic_texts` first. Only call `web_search` if Judaic texts and calendar tools cannot answer.").

### 9.3 Web-search last-resort gating (belt **and** suspenders)

1. **Prompt-level:** the `web_search` tool description and a system-prompt rule state it is last-resort, for non-halachic factual gaps only, never as a substitute for texts/poskim.
2. **Orchestration-level (hard gate, not just instruction):** the agent loop **does not expose `web_search` to the model until** at least one `search_judaic_texts` / `search_library` (and, where location-dependent, a calendar/zmanim tool) call has returned insufficient results in the current turn. Even if the model asks for it first, the orchestrator withholds it and nudges back to texts. `web_search` remains scoped to Wikipedia + the existing allowlist (§9.2) — no new provider.
3. **Attribution:** any answer that used `web_search` is tagged so the UI shows the existing general-web warning prefix (`_compose_answer_with_prefixes`, `include_web_warning=True`) and the source-attribution note marks it tier-4 / general-web. Halachic *rulings* never rest on web search alone — if only web evidence exists, the answer is framed as general background with the rabbi-consultation footer, not a p'sak.
4. **Domain allowlist** for the general provider (extend the existing whitelist logic) so web results skew reputable; respect robots/ToS.

### 9.4 Orchestration

- Implement an agent loop in `backend/ask_pipeline.py` (the unified pipeline): send the question + tool definitions → model returns either a final structured answer or `tool_use` blocks → orchestrator executes the tools (in parallel where independent, via `asyncio.gather` + `to_thread` for sync engines, reusing `_THREAD_POOL`) → feeds `tool_result` back → repeat until final answer or the round cap.
- **Backward compatibility:** keep the current pre-fetch RAG path available behind a flag (`AI_AGENTIC_TOOLS=on|off`, env-default off → on after soak). When off, behavior is exactly today's. This makes the rollout zero-breakage and instantly revertable.
- Location handling: `get_zmanim`/calendar tools need lat/lon; pull from the user's stored preference/session (already captured for the zmanim UI) and pass into the tool context; if absent, the model asks the user or `get_zmanim` returns a "location required" result rather than guessing.
- Reuse `ask_ai_async`'s existing `tool_context` param as the carrier for resolved location/community/date so tools have defaults without an extra round-trip.

### 9.5 Safety, cost, and reliability integration

- Each tool call: timeout, narrow exception catch, `health.record_success/failure(<service>)`, fail-open (a dead tool degrades gracefully — model continues with what it has).
- `cost_meter` records model tokens per round **and** any metered external calls; the per-question round cap + per-day budget alert (§E.6) prevent runaway loops.
- The §B-AGE safety classifier runs **before** the agent loop; medical/self-harm/abuse queries never enter tool-use, they route to referral.
- Prompt-injection: tool *results* (especially `web_search`) are treated as untrusted content — sanitized through the existing `_sanitize_*` path before re-injection, and the model is reminded not to follow instructions embedded in tool results.

### 9.6 Tests

- `tests/test_ai_tools.py`: each tool wrapper returns the right shape from mocked engines/APIs (offline via `conftest` fixtures); param validation (bad lat/lon, bad dates) rejected.
- `tests/test_agent_loop.py`: model-stub scenarios — (a) answerable from texts → no web_search exposed/called; (b) zmanim question → `get_zmanim` called with resolved location, deterministic times returned; (c) Hebrew-date question → `get_hebrew_date`; (d) non-Judaic factual gap → texts tried first, *then* web_search allowed, answer carries web warning; (e) round-cap enforced; (f) circuit-open provider hidden; (g) agentic-off flag reproduces today's RAG behavior exactly.
- Regression: standard halachic Q&A unchanged in quality.

### 9.7 Docs & rules

- `docs/AI_TOOLS.md`: the catalog, the hierarchy, the web-last-resort gate, location handling, and the agentic on/off flag.
- Add to `.agents/ENGINEERING_RULES.md`: "AI tool-use honors the source hierarchy — Judaic texts & computed calendar/zmanim first; `web_search` is last-resort, orchestrator-gated, never the sole basis for a halachic ruling; deterministic data (zmanim, dates, parasha, omer) always comes from the computation engines, never the model."

---

## 10. README, licensing, and GitHub profile

### 10.1 README.md — full update

Current `README.md` (281 lines) is already strong and reflects the refactored architecture (Flask+FastAPI hybrid, backend modules, 14 community datasets, env table, deployment). The update completes and aligns it with everything in this plan rather than rewriting from scratch. Add/refresh:

1. **Badges row** (top): license (MIT, §10.2), Python version, "deployed on Vercel", tests/CI status (once §F CI lands), "not legal/halachic advice" tagline.
2. **Screenshots / demo GIF** of the reader, AI answer with sources, and zmanim/calendar — the single biggest README improvement for a public project; plus the live demo link.
3. **Feature list refresh** to match shipped + planned: agentic AI tool-use (§9) once built, community-lens answers, bilingual EN/HE + RTL, PWA/offline, dark mode.
4. **AI & safety section** (new): how the multi-model RAG works at a high level, the source hierarchy (texts → poskim → whitelisted external → web last-resort), the "educational only, consult your rabbi" disclaimer, and the 13+ age-appropriateness posture (§B-AGE). Link the AI Disclosure doc (§A.3).
5. **Architecture diagram** kept current as modules move during the refactor (Phases 1–4); link `docs/SERVICE_ARCHITECTURE.md`.
6. **Environment variables** — reconcile the table against the live scan (the code reads `RATE_LIMIT_ASK`/`RATE_LIMIT_DEFAULT`, `CLERK_*`, `SUPABASE_*`, `ANTHROPIC_*`, `GEMINI_*`, `GOOGLE_API_KEY`, `SENTRY_DSN`, `STRICT_SUPABASE_RLS`, `DEPLOY_HASH`, etc.); the current README lists a couple of names that don't match (`RATE_LIMIT_PER_MIN`, `RATELIMIT_STORAGE_URI`) — fix to the real names. Mark which are required vs optional, and never include real secret values.
7. **Testing** section expanded: how to run the offline pytest suite, the coverage gate, and the golden-master approach from the refactor.
8. **Contributing** + **Code of Conduct** links (new `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`) if the repo is public.
9. **Credits & licenses** section: attribution for Sefaria, Hebcal, Wikipedia, fonts, and OSS libraries, pointing to `THIRD_PARTY_LICENSES.md` (§A.8) — important because the README is the first place people check licensing.
10. **Security policy** link (`SECURITY.md`, §A/§C) with how to report vulnerabilities.

### 10.2 Licensing — recommendation: **yes, add an MIT LICENSE (for your code), plus a NOTICES file**

**Recommendation:** add an `MIT` license for **your original code**. Reasoning, honestly weighed:

- MIT is the simplest, most permissive, best-understood OSS license — ideal for a learning/hackathon project you want people to read, fork, and learn from.
- Its built-in **"provided 'as is', without warranty… authors… not liable"** clause is a small but real layer of the liability protection you asked about in §8 — it disclaims warranty on the code itself to downstream users.
- It does **not** force your dependencies or the displayed texts open; it only governs your code.

**Important caveat (why MIT alone isn't enough):** an MIT license on your repo covers *your code*, not the **content/data** flowing through it. Sefaria texts, Hebcal data, Wikipedia (CC-BY-SA — share-alike!), the `SILEOT` font, and bundled JS/CSS libraries each carry their own licenses. So:

- Add `LICENSE` (MIT, your name + year) at repo root.
- Add `THIRD_PARTY_LICENSES.md` / `NOTICES.md` listing every third-party source and its license + attribution (ties to §A.8). Explicitly state that **content retrieved from Sefaria/Hebcal/Wikipedia remains under its own license and is not relicensed by this project's MIT license.**
- If you ever want to *require* derivatives of your web app to stay open, AGPL-3.0 would be the alternative — but that's heavier and usually unnecessary for this project. MIT is my recommendation unless you specifically want copyleft.

**Deliverable — `LICENSE` (MIT) content to drop in (fill in year + legal name):**

```
MIT License

Copyright (c) 2026 Akiva Yevdayev

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

(Also set the `license` field in any package metadata and add the MIT badge to the README.)

### 10.3 GitHub profile update (via Claude-in-Chrome connector)

**Status:** the Chrome extension is **not currently connected** (no browser instance detected), so this can't be executed yet. To run it: install/enable the Claude-in-Chrome extension and sign in, then I can drive it. I'll also need your GitHub **username** confirmed (the profile README lives in a special repo `github.com/<username>/<username>`).

**What "profile update" covers and the planned actions (with your confirmation per step, since these are public writes):**

1. **Profile README** (`<username>/<username>` repo `README.md`) — draft below.
2. **Pin** the Sh'elah repo (and up to 5 others) so it's front-and-center.
3. **Repo polish on Sh'elah itself:** description, topics/tags (`judaism`, `halacha`, `torah`, `flask`, `fastapi`, `ai`, `rag`, `sefaria`, `vercel`), website link, and ensure the new README + LICENSE render.
4. **Bio / links** on the profile (optional): one-line bio + link to the live demo.

> Per the safety rules I won't publish/commit public content without your explicit go-ahead on each write, and I'll show you exactly what will be posted first.

**Draft profile README (`<username>/<username>/README.md`) — ready to use:**

```markdown
### Hi, I'm Akiva 👋

I build things at the intersection of Jewish learning and modern software.

🔭 **Currently building [Sh'elah](https://github.com/<username>/Shelah_app)** — a
Jewish learning & halachic AI assistant. A searchable Torah library, community-aware
customs, live zmanim and a Hebrew calendar, and an AI that answers halachic questions
with cited primary sources (always pointing you back to your rabbi for a final ruling).

**Stack I reach for:** Python (Flask · FastAPI), Supabase/Postgres, Vercel, and
LLM/RAG pipelines (Anthropic Claude · Google Gemini).

- 🌱 Learning: production-grade AI app architecture, fault-tolerant backends, and a11y
- 💬 Ask me about: Torah + tech, halachic data modeling, RAG over religious texts
- 📫 Reach me: akiva.yevdayev@icloud.com

<!-- Optional: GitHub stats card -->
![Akiva's GitHub stats](https://github-readme-stats.vercel.app/api?username=<username>&show_icons=true&theme=default)
```

Replace `<username>` and tune the tone before publishing. I'll fill the real values once you confirm the username.

---

## 11. Deploy blocker — Vercel "Unmatched function pattern" (`asgi.py`) — ✅ IMPLEMENTED (Option A, 2026-07-02)

> Status: `api/index.py` created and `vercel.json` replaced with the modern `functions` + `rewrites` + `headers` form. JSON validated, entrypoint compiles, rewrite regex verified against static/app paths. Remaining: deploy to a **preview** URL and run the verification checklist below before promoting to production.

**Error:** `The pattern "asgi.py" defined in 'functions' doesn't match any Serverless Functions inside the 'api' directory.`

**Root cause (confirmed by reading current `vercel.json`):** the file mixes two **mutually incompatible** Vercel config systems:
- the **legacy** builder config — `"version": 2` + `"routes"`, and
- the **modern** config — the `"functions"` property.

The `functions` property requires each glob key to match a source file **inside the `api/` directory**. The repo has **no `api/` directory** and `asgi.py` lives at the repo root, so `"asgi.py"` under `functions` matches nothing → build fails. (The `memory: 1024` / `maxDuration: 90` you wanted is a `functions`-only feature, which is why it was added — but it can't point at a root file.)

Current (broken) `vercel.json`:
```json
{
  "version": 2,
  "routes": [
    { "src": "/static/(.*)", "dest": "/static/$1", "headers": { "Cache-Control": "public, max-age=3600" } },
    { "src": "/(.*)", "dest": "asgi.py" }
  ],
  "functions": { "asgi.py": { "memory": 1024, "maxDuration": 90 } }
}
```

### Fix — Option A (recommended): move the entrypoint into `api/`, go fully modern

This keeps the `memory`/`maxDuration` controls and uses the supported Python-on-Vercel layout.

1. **Create `api/index.py`** (thin entrypoint that re-exports the existing ASGI app — `asgi.py` is untouched, so zero application-logic risk):
   ```python
   import os
   import sys

   # Ensure repo root is importable so `asgi`, `app`, and `backend/*` resolve.
   sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

   from asgi import app  # noqa: E402  (Vercel's Python runtime serves this ASGI `app`)
   ```
2. **Replace `vercel.json`** with the modern form (note the negative-lookahead so real files in `/static` are served by the CDN, not the function):
   ```json
   {
     "$schema": "https://openapi.vercel.sh/vercel.json",
     "functions": {
       "api/index.py": { "memory": 1024, "maxDuration": 90 }
     },
     "rewrites": [
       { "source": "/((?!static/|favicon\\.ico|manifest\\.webmanifest|service-worker\\.js).*)", "destination": "/api/index" }
     ]
   }
   ```
   - Files that physically exist on disk (`/static/**`, icons) are served directly by Vercel's edge before rewrites apply; the lookahead is belt-and-suspenders so they never hit the Python function.
   - The old `Cache-Control` header on `/static` moves to a `headers` block if you still want it explicit:
     ```json
     "headers": [
       { "source": "/static/(.*)", "headers": [ { "key": "Cache-Control", "value": "public, max-age=3600" } ] }
     ]
     ```
3. **Confirm Python runtime detection:** `requirements.txt` at root makes Vercel use the Python runtime automatically (no `runtime` key needed). It already lists `fastapi`/`uvicorn`; no `mangum` is required because Vercel's Python runtime speaks ASGI natively to an exported `app`.

### Fix — Option B (minimal change): drop `functions`, use the legacy `builds`

If you'd rather not add an `api/` file, the legacy system can keep `asgi.py` at root — but you **lose** the simple `memory`/`maxDuration` knobs (those belong to the modern `functions` system):
```json
{
  "version": 2,
  "builds": [
    { "src": "asgi.py", "use": "@vercel/python" },
    { "src": "static/**", "use": "@vercel/static" }
  ],
  "routes": [
    { "src": "/static/(.*)", "dest": "/static/$1" },
    { "src": "/(.*)", "dest": "asgi.py" }
  ]
}
```
Remove the `functions` property entirely (mixing it with `builds` is the original error). Per-function memory/duration would then need the dashboard or a move to Option A.

**Recommendation: Option A** — it's the current Vercel-supported pattern, preserves your memory/duration settings, fixes the error at its root, and is forward-compatible with the §7.10 static-routing/CSP work.

### Verification (zero-breakage)
- Validate JSON (`python -c "import json;json.load(open('vercel.json'))"`).
- Deploy to a **preview** URL first (never straight to prod): confirm `/` renders, an `/api/*` route responds, `/static/css/ai.css` loads from CDN (check response headers show it wasn't function-served), and `/ask` works end-to-end.
- Confirm cold-start succeeds (the ASGI import chain `api/index.py → asgi.py → app.py → backend/*` resolves on Vercel; if a root-relative import fails, the `sys.path.insert` in step 1 is what fixes it — keep it).
- Only then promote to production.

This supersedes the brief `vercel.json` note in §3.15 / §7.10; treat §11 as the authoritative deploy-config fix.

---

## 12. PHASE 6 — Product surface, content & growth layer (external UI review, verified against codebase)

**Provenance & verification discipline.** This phase originates from an external (Grok) review of the live site. That review was produced **without codebase access**, so every claim was verified against the repo before admission. Claims that were wrong or already shipped are recorded below and **not** re-planned; claims duplicating existing plan sections are cross-referenced, not duplicated. Only validated, net-new work becomes Phase 6 scope.

### 12.0 Claim triage (verified 2026-07-02)

| Grok claim | Verdict | Evidence / disposition |
|---|---|---|
| About page returns 404 | ✅ **Correct — net-new** | No `about` route in `app.py`/blueprints; `templates/` has only `index.html` + legal pages. → §12.1 |
| Add user guides / glossary / learning paths | ✅ Correct — net-new (scoped down) | No help/guide/glossary template exists. → §12.2 |
| "Loading..." indicators in Community Customs | ✅ Correct — **already planned** | 22 `Loading` occurrences in `index.html`; fix is the §7 loading-states overhaul (skeletons + aria). Phase 6 adds only the customs data-reliability check (§12.3). |
| Improve search/filters/breadcrumbs/discoverability | ✅ Partially correct — net-new (scoped) | Library search exists (`search_library`, `/api` search routes); breadcrumbs/filters in the reader UI are net-new. → §12.3 |
| Add bookmarking / note-taking / export | ❌ **Mostly wrong — already shipped** | Bookmarks (`routes_user`), ask history (`_store_ask_history`), chapter export (`/api/export/chapter`, docx/pdf) all exist. Net-new residue: per-answer export + notes are **deferred** (§12.6). |
| Add user accounts for saved sh'elahs | ❌ **Wrong — already shipped** | Clerk auth + Supabase-backed bookmarks/history/preferences exist. No action. |
| Offer answer modes (beginner-friendly, strict) | ❌ **Mostly wrong — already shipped** | Modes `balanced/practical/sources/strict` + `SIMPLE_SYSTEM_PROMPT` exist. No action. |
| Enhance Shul mode | ⚠️ Exists (toggle + auto-scroll + speed in reader) | Enhancement is real but unspecified; **deferred** pending user feedback data (§12.4). |
| Citation links on AI sources | ⚠️ Partial — net-new (small) | Sources render with refs (§7 source-box fix covers rendering); deep-linking a citation into the reader at that ref is net-new. → §12.3.3 |
| Disclaimers, guardrails, rabbinic consultation | ✅ Correct — **already planned** | §8.B / §8.B-AGE in full. No new scope. |
| WCAG, mobile, tap targets, contrast | ✅ Correct — **already planned** | §7, §8.F.2, `ENGINEERING_RULES.md`. No new scope. |
| User feedback mechanism for synthesis accuracy | ✅ **Correct — net-new** | No feedback capture exists anywhere in routes/UI. → §12.4 |
| SEO: meta, structured data, sitemap, keywords | ✅ Correct — net-new beyond §7.10 | §7.10 covers meta/OG only; **no** `sitemap.xml`, `robots.txt`, or JSON-LD exists. → §12.5 |
| Analytics (e.g., Google Analytics) | ⚠️ Correct need, wrong default | No analytics exist; but §8.A.4 gates non-essential analytics behind consent (EU). Use a **cookieless, privacy-first** option, not GA-by-default. → §12.5.3 |
| Performance monitoring / System Inspector | ✅ **Already planned/shipped** | Devtools + §8.E observability. No new scope. |
| GDPR/privacy/backups/security | ✅ **Already planned** | §8.C/D/E in full. No new scope. |
| Forums, mobile app, public API, partnerships, newsletters, monetization, more languages | ⚠️ Out of scope now | Real long-term options; each carries major cost (moderation liability, app-store overhead, API abuse surface). **Deferred registry** (§12.6), not planned work. |

### 12.1 About page (`/about`) — trust & transparency

1. New `templates/about.html` on the existing legal-page pattern (`legal_topbar`, bilingual en/he, both themes, token-styled), route added in the appropriate blueprint (not `app.py`, per `ENGINEERING_RULES.md`).
2. Content: mission; who builds it (solo developer — honest, no invented "team"); sourcing methodology (Sefaria/Hebcal/whitelisted corpora + RAG, linking the AI Disclosure doc §8.A.3); AI synthesis at a high level; update cadence; the non-binding/consult-your-rabbi posture (§8.B); contact. Reuses §8 language — the About page **links** to ToS/Privacy/AI-Disclosure rather than restating them (single source of truth).
3. Footer/nav link added wherever the legal links render.

### 12.2 Learner-facing help content

1. **`/help` user guide** (same template pattern): how to ask a good sh'elah, what the answer modes mean, reader features (including the existing Shul Mode, bookmarks, export), community-lens explanation, zmanim/calendar tour. Bilingual.
2. **Glossary**: a curated static glossary page of recurring halachic terms (kezayit, muktzeh, eruv, …) — seeded from the existing lexicon capability (`_lookup_hebrew_word_meaning` engine and, once §9 ships, the `lookup_word_meaning`/`convert_measurements` tools) so definitions match what the AI itself uses. Static-generated (build-time JSON → template), not a per-view API fan-out.
3. Learning paths (beginner/intermediate/advanced) — **deferred** (§12.6): real curriculum design, not a template task.

### 12.3 Library discoverability & reader polish

1. **Breadcrumbs** in the reader/library view reflecting the Sefaria category tree (`get_library_index` already exposes the hierarchy) — server-provided data, client-rendered, ARIA `nav[aria-label="breadcrumb"]`.
2. **Category/corpus filters** on library search results (the backend `search_library(query, filters)` already accepts filters — expose them in the UI).
3. **Citation deep-links**: each AI-cited source (`ai_cited_sources`, `_compact_ai_sources` output) links into the reader at that ref (route pattern already exists for refs; wire the click target). Coordinates with the §7 source-box fix — build on its delegated-listener container.
4. **Customs section reliability**: verify the customs region's data path (`routes_community`, `_retrieve_community_knowledge`) has the Phase-3 circuit-breaker + fail-open behavior end-to-end, and its loading UI is converted by the §7 skeleton work. No parallel fix — just close the loop.
5. Semantic/AI-enhanced search — **deferred** (§12.6): embedding infrastructure is a cost/complexity step-change; current keyword+glossary search (§4 Phase 2) plus filters must be measured first.

### 12.4 Answer feedback loop (synthesis accuracy)

1. Per-answer feedback control (helpful / not helpful + optional free-text ≤500 chars) on AI answers. POST to a new `backend/routes_*` endpoint; store in a Supabase `answer_feedback` table (user_id nullable for anonymous, question hash, answer metadata: mode/lens/language/`fallback`/`safety_class`, verdict, comment, ts). RLS per §8.C.2.
2. Rate-limited (reuse existing limiter), input-sanitized, no PII invited in the free-text prompt ("don't include personal details").
3. Surfaced in devtools: a simple feedback digest view so accuracy problems become visible (feeds §8.F.3 content-QA).
4. Shul-mode and reader-feature enhancements are **deferred until this loop produces data** — build the measuring instrument before renovating on guesswork.

### 12.5 SEO & privacy-respecting analytics

1. **`robots.txt` + `sitemap.xml`**: static `robots.txt` (allow public pages, disallow `/api/`, devtools); generated sitemap covering the stable public routes (home, about, help, glossary, legal pages, parasha page — *not* per-ref library URLs initially). Served as static files (works with the §11 rewrite's static-first behavior — verify they're excluded from the function rewrite like other root static assets, extending the §11 negative-lookahead if needed).
2. **Structured data (JSON-LD)**: `WebSite` + `Organization` on the home page; `FAQPage`/`Article` only where content genuinely matches (no schema spam). Coordinates with §7.10's meta/OG work.
3. **Analytics — consent-gated, cookieless-first**: per §8.A.4, default to a cookieless privacy-first option (e.g. Vercel Web Analytics or self-hosted Plausible) documented in the Privacy Policy; anything cookie-based requires the consent gate first. Track only aggregate usage (page views, ask counts) — never question content tied to identity beyond what §8.A.2 already discloses.
4. Keyword/copy pass on public pages ("halachic questions with sources", "minhagim", "Torah library") — light-touch, no keyword stuffing.

### 12.6 Deferred registry (explicitly not planned — revisit post-launch with data)

Community forums (moderation + liability program of its own); native mobile app (PWA already exists — measure installs first); public API access (abuse/cost surface, needs keys/quotas/terms); newsletters/partnerships/social (marketing, not engineering); monetization/donations (§8.G.5 trigger); additional languages beyond en/he; semantic search embeddings; learning paths; per-answer notes & answer-export; Shul-mode enhancements (pending §12.4 data).

### 12.7 Tests & exit criteria

- Route tests for `/about`, `/help`, glossary, `robots.txt`, `sitemap.xml` (200, correct content-type, bilingual toggles render).
- Feedback endpoint: happy path, rate-limit, oversize/HTML-injection input rejected, RLS denies cross-user reads.
- Citation deep-link: cited ref navigates to the correct reader location (client test or route-level assertion on the generated href).
- Sitemap validates against the sitemap schema; JSON-LD passes Google's structured-data linting rules.
- Full suite green; `graphify update .`; new routes live in blueprints, never `app.py`.

**Sequencing:** §12 runs after the §7 frontend overhauls (it builds on the token layer, skeletons, and source-box fix) and assumes §8.A legal pages exist to link to. Quick wins (12.1, 12.5.1) can run any time — they touch nothing shared.

---

## 13. PHASE 7 — SonarCloud static-analysis debt (deferred structural findings)

**Provenance.** SonarCloud CI scan of commit `de1f3a2` returned 297 issues. The mechanical bucket (unused vars, duplicate literals, comprehension idioms, missing FastAPI docs, dead code, a real URL-injection fix, easy a11y renames, CSS contrast/formatting fixes) was triaged and fixed directly in the same pass that added this section — not tracked here. The two buckets below were deliberately **deferred** because they are behavior-touching refactors, not mechanical fixes, and warrant their own review cycle rather than being bundled into a bulk lint-fix commit.

### 13.1 Cognitive-complexity refactor (`python:S3776`, 67 instances)

Concentrated in `app.py`, `backend/sefaria_library.py`, `backend/helpers.py`, `backend/routes_library.py`, `asgi.py` — the same procedural core already targeted by §4 Phases 1–4 (text/formatting extraction, retrieval/corpus-matching extraction, `app.py` streamlining). Rather than chasing the Sonar threshold function-by-function, this bucket should be **retired as a side effect of §4**, not attacked directly:

1. Re-run the SonarCloud scan after §4 Phases 1–4 land and diff the S3776 list — expect the majority of `app.py`-resident complexity hits to disappear once `_normalize_ai_answer`/`_format_ui_answer`/keyword-extraction/corpus-matching move into `backend/utils/text_engine.py` and `backend/utils/search_provider.py` (smaller, single-purpose functions naturally drop below the complexity-15 threshold).
2. For whatever remains after §4 (expected: `sefaria_library.py` catalog/index-building functions, a handful of `routes_library.py` handlers), triage individually: each refactor is its own reviewed change — extract guard clauses first, then nested-conditional flattening, then early-return conversion — never a blind mechanical split. Test-anchor every extraction (existing suite must stay green at each step; no behavior change).
3. **Do not attempt in one bulk pass.** A 67-site simultaneous refactor across 5 files is exactly the kind of diff that hides a regression. Sequence it as its own phase, function-by-function, after §4 ships.

### 13.2 Full native `<dialog>` migration (`Web:S6819`, 3 remaining instances)

The 5 low-risk `role="status"` → `<output>` renames were fixed in the same pass as §13's mechanical bucket. The 3 real modals — `#calendarModal`, `#chapterGridModal`, `#legalModal` (`templates/index.html`) — still use `role="dialog"` + manual show/hide JS and are **deferred**:

1. Convert each to a native `<dialog>` element; replace the manual `display`/class-toggle open logic with `.showModal()` and close logic with `.close()`.
2. Native `<dialog>` provides browser-default focus-trap and `::backdrop` — audit existing custom backdrop-click-to-close and Escape-key handlers for double-handling once the native behavior is live (native `<dialog>` already closes on Escape; a duplicate handler would double-fire).
3. Re-test each modal's full interaction surface in-browser before merging: open trigger, close button, backdrop click, Escape key, focus returns to the trigger element on close, and screen-reader announcement on open (matches the WCAG dialog pattern this rule exists to enforce).
4. Ship as three independent small PRs/commits (one per modal), not one combined diff — each has a distinct trigger/close call-site surface and independent regression risk.

**Exit criteria for this section:** re-run SonarCloud after §4 lands to confirm the S3776 count has dropped as predicted before doing manual refactors on what's left; all 3 modals interaction-tested in-browser (not just visually) before each commit.

---

## 14. PHASE 8 — Vercel Fluid Compute "Active CPU" cost fix (billed with no active user) — 💸 live cost bleed, independent, run first

**Provenance & source.** Root-caused by re-reading the repo against Vercel's official Fluid Compute pricing/optimization guidance (`vercel.com/docs/pricing/manage-and-optimize-usage`, plus the Active CPU pricing announcement and the "reduce Serverless Execution usage" KB article) and an Explore-agent sweep of the whole codebase for cron jobs, background loops, timers, and unawaited tasks. **Conclusion: there is no rogue loop, cron, or orphaned background task in this codebase** — `.github/workflows/ci.yml` has no `schedule:` trigger, there's no `apscheduler`/`celery`/`schedule` dependency, and every `asyncio.create_task` in `asgi.py`/`backend/ask_pipeline.py` is fanned out and immediately joined via `asyncio.gather` inside the same request (nothing fire-and-forget). The bleed is architectural, not a stray process: **the app amplifies every single pageview — including bots, crawlers, link-unfurlers, and idle tabs the user doesn't think of as "active" — into ~19 fully-billed serverless invocations of content that is identical for every visitor on a given day, and none of it is cacheable at Vercel's CDN because of one blanket header.**

### 14.1 How Vercel bills this (ground truth from the docs)

- Fluid compute's **Active CPU pricing** (default on all Hobby/Pro/new-Enterprise projects) bills **CPU time only while your code is actually executing** — I/O wait (DB calls, upstream API calls) does not accrue Active CPU. But **Provisioned Memory bills continuously for the life of the instance** (until the last in-flight request finishes), and — critically — **each serverless invocation itself is real, billed work**: routing, JSON (de)serialization, template rendering, and any in-process cache lookups all run on-CPU even when the "real" work (e.g. a Sefaria fetch) is cheap or cached. So **invocation *count* is the lever**, not just per-request duration.
- Vercel's own optimization guidance boils down to: (1) **cache aggressively at the CDN** via `Cache-Control`/`stale-while-revalidate` so repeat requests for the same content never reach the function at all; (2) **reduce invocation count** (ISR/SSG over uncached SSR, fewer redundant fan-out calls); (3) **co-locate the function with its dependencies** (DB/API region) so the CPU-bound part of each request finishes faster; (4) **monitor the Observability/Usage tab, broken down by route/region**, to find the actual driver instead of guessing.
- None of this requires a paid feature or infra change — it's `Cache-Control` headers, one client-side gate, and a `vercel.json` tweak.

### 14.2 Root causes found (verified against the current tree, not last quarter's)

1. **Blanket `Cache-Control: no-store` on every `/api/*` route** — `app.py::apply_response_cache_policy` (L2599-2601):
   ```python
   if path.startswith("/api/") or path in {"/ask", "/set_location"}:
       response.headers["Cache-Control"] = "no-store"
       return response
   ```
   This is correct for personalized/dynamic routes (`/ask`, `/api/user/*`, `/api/bookmarks/*`, `/api/auth/*` — the service worker already enumerates exactly this private set as `PRIVATE_API_PREFIXES` in `static/service-worker.js`), but it also blankets **non-personalized, same-for-everyone-today endpoints** — `/api/daily-study` (`backend/routes_calendar.py` L76-77) and `/api/text/<ref>` (library text lookup). Because the header is `no-store`, Vercel's CDN/Edge can **never** short-circuit a repeat request — every hit, from every visitor and every bot, must cold/warm-invoke the function and re-run Python, even though `backend/sefaria.py` already TTL-caches the daily-study payload for 5 minutes *in one instance's memory* (`_DAILY_STUDY_CACHE = TTLCache(ttl=60*5)`, L22) — that cache saves the upstream Sefaria call, but does nothing to stop the invocation (and its Active CPU) from happening in the first place.
2. **`installDailyPrewarm()` fan-out fires on every page load, unconditionally** — `static/js/zmanim.js` L91-130. 2.2s after `load`, it calls `/api/daily-study` then `Promise.allSettled`-prefetches **every ref returned, ×2 variants each** (`prefetchRefText`, L91-97) — up to **~19 backend requests per single page view**. There is no check for "did I already prewarm this today" or "is this session already warm" — a tab left open and reloaded, a bot/crawler that executes JS (many do: Googlebot, link-preview bots, AI-agent crawlers), or a returning visitor who's done nothing but open the site all re-trigger the full ~19-call fan-out. Combined with #1, none of those 19 calls can ever be CDN-cached, so this is the single biggest multiplier of billed invocations relative to genuine "someone is actively using the app right now" traffic.
3. **No `robots.txt`** anywhere in the repo (confirmed absent), and `vercel.json`'s rewrite is a near-catch-all (`"source": "/((?!static/|favicon\\.ico|manifest\\.webmanifest|service-worker\\.js).*)"` → `/api/index`). Every crawler/bot hit — with no disallow rules to steer them away from expensive paths — becomes a full function invocation, and (per #2) a JS-executing bot multiplies into ~19. This item is already tracked as a nice-to-have in §12.5 (SEO); this section elevates it because it is now a direct, verified cost driver, not just an SEO gap.
4. **No `regions` pin on the function** in `vercel.json` — Vercel's own guidance is that co-locating the function with its primary dependencies (Supabase, and the outbound calls to Sefaria/Anthropic/Gemini) shortens the on-CPU portion of every request. Currently unset, so Vercel picks a default.
5. **`maxDuration: 90`** (`vercel.json`) is a generous ceiling — not itself an idle-billing cause, but it means a stalled external call can burn up to 90s of billed execution before failing. Worth tightening once §4 Phase 3's circuit-breaker/per-call-timeout work (already planned) lands; not blocking for this section.
6. **Outside the repo's control, must be checked manually (no code fix possible):** confirm in the Vercel dashboard that nothing external — an uptime monitor (UptimeRobot/healthchecks.io/etc.), a preview-deployment build left running, or a stale project — is polling the site. Nothing in this codebase or `.github/workflows/ci.yml` does this (verified), so if it's happening it's configured outside the repo.

### 14.3 The fix (ordered, each step independently shippable)

1. **Split `/api/*` cache policy into "private" vs "public/cacheable"** in `app.py::apply_response_cache_policy`, reusing the existing `PRIVATE_API_PREFIXES` concept from `static/service-worker.js` as the source of truth for what must stay `no-store` (`/api/user/`, `/api/bookmarks/`, `/api/auth/`, `/api/client-errors`, plus `/ask` and `/set_location`). Every other `/api/*` GET that returns identical content for a given cache key (`/api/daily-study`, `/api/text/<ref>`) gets a real `Cache-Control: public, max-age=<N>, stale-while-revalidate=<M>` — mirror the existing `/static/` pattern (`RESOURCE_RELOAD_SECONDS` / `STATIC_STALE_WHILE_REVALIDATE_SECONDS`, same file) rather than inventing new constants. Daily-study content changes once per calendar day, so its `max-age` can safely be generous (e.g. an hour, with a longer `stale-while-revalidate`); library `/api/text/<ref>` content is effectively immutable per ref, so it can be cached long with revalidation. This is the change that lets Vercel's CDN actually absorb repeat/bot traffic instead of every hit reaching the function.
2. **Gate `installDailyPrewarm()` to once per calendar day per client** (`static/js/zmanim.js`): before firing, check a `localStorage` (or the existing `state.js` store) flag keyed by today's local date; if already prewarmed today, skip the whole fan-out. This alone kills the ~19x multiplication for any repeat load/reload/bot re-hit within the same day — the CDN caching in step 1 then absorbs whatever's left.
3. **Add `robots.txt`** (static, served outside the function — add it to `vercel.json`'s rewrite negative-lookahead alongside `favicon.ico`/`manifest.webmanifest`/`service-worker.js` so it isn't itself routed through `/api/index`): allow the public pages, disallow `/api/`, `/devtools`, and any admin-ish paths. This is the same deliverable already scoped in §12.5.1 — do it here first since it's now a cost fix, not just SEO; §12.5 can skip re-doing it.
4. **Pin `regions` in `vercel.json`**'s `functions["api/index.py"]` (or top-level `regions`) to the region closest to the Supabase project and, ideally, the bulk of real users — check the Supabase project region in the dashboard and match it.
5. **Verify Fluid Compute + Active CPU pricing toggle** in the Vercel project dashboard (Settings → Functions) — this is a project-level dashboard setting, not expressible in `vercel.json`, so it's a manual check, not a code change.
6. **Check the Usage/Observability tab** (Vercel dashboard) broken down by route and by region to confirm, after steps 1-4 ship, that `/api/daily-study` and `/api/text/*` invocation counts actually dropped, and to rule out external uptime-monitor traffic (item 14.2.6) as a residual cause.

### 14.4 Tests & exit criteria

- `tests/test_routes_calendar.py` (or nearest existing route-test module): assert `/api/daily-study` and `/api/text/<ref>` responses now carry `Cache-Control: public, max-age=..., stale-while-revalidate=...` instead of `no-store`; assert the private routes (`/ask`, `/api/user/*`, `/api/bookmarks/*`, `/api/auth/*`, `/api/client-errors`, `/set_location`) are unchanged (`no-store`).
- Manual/browser check: load the site twice within the same simulated day (or fake the `localStorage` date key) and confirm `prewarmDailyStudy()`'s network fan-out fires once, not on every load.
- `robots.txt` reachable at `/robots.txt` and **not** rewritten into `/api/index` (check response comes from the static/CDN path, not the function — same verification pattern already used in §11's checklist).
- Full `pytest -q` green; `graphify update .` after.
- Post-deploy (on a **preview** URL first, same discipline as §11): watch the Vercel dashboard's Usage/Observability tab for 24-48h and confirm Active CPU / invocation count on `/api/daily-study` and `/api/text/*` dropped materially versus the pre-fix baseline.

**This section can run independently and first** — like §11, it's not gated on the §4 backend refactor and directly stops live cost bleed.

---

**Awaiting your command to begin Phase 1.** No code will be written until then. The §8 legal documents will be drafted for **attorney review** — they are not a substitute for a lawyer. The §10.3 GitHub profile update needs the Chrome extension connected + your username before I can execute it. **§11 is a live deploy blocker — fixing `vercel.json` can be done independently and first if you want the site deploying again before the refactor begins.**
