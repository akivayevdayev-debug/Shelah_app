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

The following items from the prior roadmap remain valid and are **not** part of the backend refactor above. They are parked here, unchanged in intent, to run *after* the four-phase backend work (or independently, since they touch disjoint files). Summarized to keep this document focused. Status verified 2026-07-31 (§18 has the full evidence trail — these one-liners are the executive summary):

- **AI source box fix** — `innerHTML +=` in render loops destroys earlier click handlers; inconsistent `escapeHtml`; duplicate `.ai-source-card` styles; capped nth-child stagger. → single-insert + delegated listeners + per-box `--i` stagger + skeletons. **⚠️ Partial.** Single-insert + `--i` stagger + skeleton done; listeners attach per-anchor after the write (not container-delegated); the deduped `.ai-source-card` CSS is dead (JS renders `.ai-source-box`, styled separately in two files); skeleton region lacks `role="status"`.
- **Observability** — structured JSON logging with request IDs across threads/asyncio, Sentry via `_capture_backend_error`, `cost_meter.py` token/USD logging to Supabase (note: `cost_meter.py` now exists — verify it covers all model call sites). **✅ Done**, including the browser-side Sentry SDK + `/api/client-errors` hardening (was tracked separately as Prompt 6b — now merged).
- **Docs pass** — README, `SERVICE_ARCHITECTURE.md`, new `API.md`/`OBSERVABILITY.md`/`FRONTEND.md`, env-var table. **✅ Done**, though README does not yet cross-link the four new docs (small gap, see §18).
- **Motion overhaul** — vanilla `motion` (motion.dev) on current surfaces, Framer Motion on any future React islands, via the `ui-ux-pro-max` tool workflow; zero new `@keyframes` outside the token layer (rules in `.agents/ENGINEERING_RULES.md`). **⚠️ Partial.** Token layer + keyframe consolidation done; `window.ShelahMotion` wrapper is real, but the motion.dev/Motion One library it wraps is **never loaded** (no CDN `<script>` anywhere) — every animation call silently runs the instant-fallback path in production today. `prefers-reduced-motion` and hover/focus states solid but not universal.
- **Dark mode overhaul** — collapse the ~570 scattered `body.theme-dark` overrides into a semantic token layer with `prefers-color-scheme` + no-FOUC head script; AA-contrast audit both themes; 21st.dev component patterns as QA baseline. **✅ Done as scoped, see §18 (superseded the "Largely not done" note below — kept for history).** ~~❌ Largely not done.~~ ~~`.theme-dark` override count is 583 (baseline was ~570-574) — essentially unmigrated; the semantic token layer itself (`tokens.css`) is genuinely built.~~ 7 of 8 feature sheets migrated to the token layer as of 2026-08-01 (`style.css` deliberately left, a scoped decision — see §18.1 Prompt 9); the no-FOUC script had an active bug (read a dead `localStorage` key, forcing dark mode for every user regardless of saved preference) — **fixed 2026-07-31**; AA contrast audit landed with real computed ratios and its 2 genuine findings resolved 2026-08-15 (`docs/ACCESSIBILITY_AUDIT.md`). No contrast audit evidence found anywhere.
- **Loading-states overhaul (light + dark)** — token-driven loading design system; AI loading animation rebuilt off hardcoded hex with dark variants + timer cleanup; skeletons for all "Loading..." text regions; `role="status"`/`aria-live`; reduced-motion guards. **⚠️ Partial.** AI-answer loading path is genuinely done (tokens, timer cleanup on all exit paths, reduced-motion). Three regions (`#prayerBooksNavList`, `#popularTextsGrid`, `#selectionInsightsHint`) still bare "Loading…" text with no skeleton/ARIA. The shared shimmer keyframe (`tokens.css`'s `shelah-shimmer`) animates `background-position`, violating the file's own "transform/opacity only" rule.
- **Frontend platform fixes** — replace the production Tailwind CDN JIT compiler with a build step (kills the CSP `'unsafe-eval'`), SRI/version-pin CDN scripts, font preconnect + woff2, SEO/OG metadata, dynamic `lang`/`dir` for Hebrew. **✅ Done as scoped, see §18.1 Prompt 11 (superseded the "Partial" note below — kept for history).** ~~⚠️ Partial.~~ Tailwind build step done, CDN gone, `'unsafe-eval'` gone, `Strict-Transport-Security` added, `terms.html`/`privacy.html`/`accessibility.html` now have font preconnect + OG/canonical metadata, `lang`/`dir` toggle fixed (was `dir`-only, now sets both). Nonce-based `script-src`/`style-src` deliberately deferred (confirmed with the user 2026-08-15, see Prompt 31 — 112 inline `onclick=` + 43 inline `style=` sites in `templates/index.html` make the refactor large/high-risk; CSP2+ browsers drop `'unsafe-inline'` entirely once a nonce is present, so a partial change breaks every handler). Google Fonts SRI confirmed permanently inapplicable (Google serves different `@font-face` payloads per User-Agent — no single static hash is valid).

- **Inline-block / ES-module duplication (frontend instance of §2)** — `templates/index.html`'s 10,004-line inline `<script>` (lines 1568–11571, 314 top-level functions) runs its own `POST /ask` and its own semantic-bookmark save alongside the `static/js/` modules that already own both, and never once uses the `window.ShelahModules` bridge `docs/FRONTEND.md` documents as the migration mechanism (zero consumers repo-wide). **⚠️ New, found 2026-08-17 — full finding and phased plan in §19, tracked as Prompt 32.** Note the direction of drift is counter-intuitive: the inline `/ask` copy is the *stronger* implementation (retry + timeout), so "delete inline, call the module" is a regression, not a cleanup. `docs/FRONTEND.md` itself is substantially divergent from the shipped modules (§19.5).

Full detail for these lives in version history; the backend four-phase plan above is the active, commanded work.

---

## 8. Production-readiness program — everything required to launch safely

> **Not legal advice.** I am not a lawyer and this is not legal advice. Sh'elah gives religious/halachic guidance via AI and processes personal data across jurisdictions — a combination with real liability exposure. Every legal document and disclosure below is **draft-level scaffolding to hand to a licensed attorney** (ideally one familiar with software, AI, and consumer-privacy law in your operating jurisdiction). Treat attorney review as a hard launch gate, not optional polish. The engineering items are mine to implement; the legal items I can draft, but a human lawyer signs off.

This program runs in parallel with (and after) the backend refactor. It is grouped into workstreams A–H with concrete deliverables. Each item is a checklist line so it can be tracked.

### A. Legal documents & disclosures (attorney-review gated)

**Engineering status: ✅ drafted 2026-08-16 — attorney review still pending, per this section's own preamble.** All 8 deliverables below are implemented as DRAFT scaffolding (`templates/terms.html` rewritten to 20 sections, `templates/privacy.html` expanded, new `templates/ai-disclosure.html`/`acceptable-use.html`/`dmca.html`/`licenses.html`, `THIRD_PARTY_LICENSES.md`). See `claude_code_prompts.md` Prompt 12's status-table row for the full evidence trail. This does **not** move the §8.H launch-gate checkbox to done — that requires the attorney review itself, which is out of scope for an engineering pass.

**Consent mechanism update 2026-08-16:** the original checkbox-gated modal (`#legalModal`, click-through, POSTed to `/api/accept-legal` on accept) was replaced at the user's request with a permanent, non-blocking footer (`.site-footer` in `templates/index.html`) stating the ToS/Privacy/age requirement as plain notice text — no checkboxes, no accept/decline gate. This is a **browsewrap**, not clickwrap, notice; per industry practice (see research summary in `claude_code_prompts.md`), browsewrap is standard for anonymous/logged-out browsing but weaker evidence of consent than clickwrap at account creation. `/api/accept-legal` and the `LEGAL_TERMS_VERSION`/`LEGAL_PRIVACY_VERSION`/`legal_terms_version`/`legal_privacy_version` machinery in `app.py`/`backend/routes_user.py` still exist and are fully tested, but nothing in the current UI calls them — they're dormant infrastructure, ready to wire into a real clickwrap moment (e.g., a consent checkbox at Clerk sign-up) if/when that's built. Flag this gap to counsel during attorney review: whether browsewrap-only notice is sufficient for this Service's age-gating and data-collection profile, or whether clickwrap at sign-up should be added.

Current state (scanned, historical — see engineering-status note above for what has since shipped): `terms.html` exists with 11 sections **but is missing §6, §10, §12** (numbering jumps — likely deleted content), and `privacy.html` has 9 sections including a GDPR/CCPA rights stub. Both need substantial expansion. Deliverables:

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

**Engineering status: ✅ done 2026-08-17 — see `docs/SECURITY.md` for the full findings report and `claude_code_prompts.md` Prompt 15's status row for the evidence trail.** One critical, action-required item remains for the repo owner (not resolvable by an engineering pass): a live API key leaked in git history needs rotation, and a decision on whether to rewrite history to purge it.

1. **Secrets & key management.** Scan confirms many secrets via env (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `FLASK_SECRET_KEY`, Clerk, Supabase). Verify: no secrets committed (`git log`/`git secrets` scan, check `.env` never tracked — `.gitignore` covers it, confirm history is clean; if any key ever touched a commit, **rotate it**); `FLASK_SECRET_KEY` is strong/random in prod; least-privilege Supabase keys (anon vs service-role separation — never ship service-role to the client).
2. **Supabase Row-Level Security.** You have `scripts/sql/SUPABASE_RLS_POLICIES.sql` and a `STRICT_SUPABASE_RLS` flag and `/api/devtools/rls-audit`. Make strict RLS the **enforced default in prod**, run the audit in CI, and verify every user-data table (bookmarks, preferences, memory summaries) denies cross-user access. This is both a security and a privacy-law requirement. **⚠️ Corrected 2026-08-19 — this item's premise is unverified. → §21.** It assumes RLS currently works. RLS depends on `auth.uid()` resolving a Clerk JWT, which requires a Supabase dashboard setting that exists nowhere in this repo and has never been confirmed. If it is not set, RLS is not a *weak* backstop — it silently returns zero rows on all four routes using the user-scoped client. §21 resolves this with an operator action plus a live acceptance test; this checkbox is not reachable until §21.2.1 has an answer.
3. **CSP hardening** (already in roadmap §7.10): remove `'unsafe-eval'` (kill the Tailwind CDN compiler), move to nonce-based `script-src`, add `Strict-Transport-Security` (HSTS), `Permissions-Policy`; drop deprecated `X-XSS-Protection`.
4. **Dependency & supply-chain:** SRI/pin all CDN scripts (§7.10); `pip-audit`/Dependabot on `requirements.txt`; SCA in CI; the `.pre-commit-config.yaml` already present — extend it with secret-scanning (`detect-secrets`/`gitleaks`).
5. **Input validation & abuse:** prompt-injection defenses exist in `claude.py` — keep as defense-in-depth; ensure all route inputs are validated (coordinate bounds, ref decoding, payload caps already partially present); confirm the rate limiters (Flask-Limiter + ASGI deque) are active in prod and consider a shared store since per-instance limits are weak on serverless. **→ §16, and specifically §16.8 for what has actually shipped as of 2026-08-19.** Note the phrasing above ("Flask-Limiter **+** ASGI deque") describes exactly the two-independent-limiters state §16.3 explicitly rejected; §16.8 records how that came about and what closing it requires.
6. **AuthN/Z review:** Clerk JWT verification path (`_verify_clerk_token`), `CLERK_ENFORCE_AUTH` on for protected routes in prod, token-in-cookie handling reviewed, session cookie flags (`Secure`, `HttpOnly`, `SameSite`) verified in `apply_session_cookie_policy`.
7. **Penetration test / security review** before launch (the `security-review` skill or an external pass); document findings + remediation.

### D. Privacy operations (makes the policy real, not just words)

**Engineering status: ✅ done as scoped 2026-08-20 — see `claude_code_prompts.md` Prompt 16's status row and `docs/PRIVACY_OPERATIONS.md`/`docs/DPIA.md` for the full evidence trail.** All 6 deliverables below were already substantially shipped before this pass (DSR export/delete, consent versioning, DPA checklist, RoPA, retention cron, breach plan); this pass closed the one real remaining gap — `_delete_clerk_user()` now reports a silent-partial-failure to Sentry instead of only an HTTP response field. Two items remain genuinely operator-side, not engineering: `CRON_SECRET` still ships empty in `.env.example` (§24.4), and the DSR endpoints' deliberate RLS bypass is tracked at §21/Prompt 34, not here.

1. **Data-subject request (DSR) flow:** a working path for access/export/delete — at minimum an email intake + documented internal procedure; ideally a self-serve "download my data" and "delete my account + data" in settings (deletion must cascade through Supabase tables and Clerk). **✅ Shipped — verified 2026-08-19:** `GET /api/user/data-export` (`backend/routes_privacy.py:114`) and `POST /api/user/delete-account` (`:196`, Supabase-first then Clerk-identity-last) both exist. **Two caveats, both real:** these endpoints deliberately use the **service-role** client, routing around RLS — the module docstring (`:13-16`) says gating them behind the user-scoped client "would 403 for exactly the users this feature exists to serve," which is also §21's strongest evidence that RLS does not currently resolve. And `CLERK_SECRET_KEY` absence makes the Clerk-identity-deletion step a **silent per-request partial failure** rather than a boot-time hard requirement, despite gating the identity-deletion half of a GDPR-relevant flow. Fix that gating before marking this line done.
2. **Consent records:** store ToS/Privacy version + timestamp at acceptance (wire through existing `/api/accept-legal`); re-prompt on material version changes.
3. **Data Processing Agreements (DPAs):** execute/accept DPAs with each processor (Anthropic, Google, Clerk, Supabase, Vercel, Sentry) — most offer click-through DPAs; keep copies.
4. **Records of Processing (GDPR Art. 30)** and a **basic DPIA** given you process data to drive automated religious guidance.
5. **Retention enforcement:** scheduled job actually deleting data past its retention window (not just a policy promise). **✅ Shipped — verified 2026-08-19:** `GET /api/devtools/retention-enforce` (`backend/routes_privacy.py:272`), `CRON_SECRET`-gated, enforcing 90-day windows on `ask_history` and `ai_usage_log` (`:66-67`), scheduled daily at 14:00 UTC. **Three caveats:** the cron entry lives only in an **uncommitted** `vercel.json` (§24.3); `CRON_SECRET` ships empty in `.env.example:102` (§24.4), so whether the route is gated and whether the cron authenticates are both unverified; and `docs/DATABASE.md:133-140`'s retention table names tables that do not exist while omitting both tables this job actually sweeps (§23.1).
6. **Breach response plan:** who, what timeline (GDPR 72h), notification templates.

### E. Reliability, observability & operations

**Engineering status: ✅ done as scoped 2026-08-20 — see `claude_code_prompts.md` Prompt 17's status row and `docs/RUNBOOKS.md` for the full evidence trail.** `docs/RUNBOOKS.md` gained the six sections it was previously missing (uptime monitoring, backups & recovery, incident response, rollback, deploy checklist, rate-limit/cost-ceiling status), and the `hebcal` circuit breaker (registered in `backend/health_check.py` since Phase 3 but never actually called) is now wired into all four call sites. **One real gap found and deliberately left open, catalogued at §26:** `health.is_healthy('claude')`/`('gemini')` is never consulted before the *primary* `/ask` AI call — only the fallback stage is circuit-breaker-gated.

1. **Observability finished** (roadmap §7.3 / partly built — `SENTRY_DSN`, `cost_meter.py`, `logging_setup.py` exist): confirm Sentry initialized in prod, structured request-ID logging across Flask + asyncio, cost metering on every model call with a budget alert, and the circuit breakers (§Phase 3) wired on all external calls.
2. **Health checks & uptime monitoring:** `/api/health` + `/api/stack/health` exist — wire an external uptime monitor + alerting.
3. **Error budgets & graceful degradation:** verify the fail-open fallback (local corpus) path end-to-end; user-facing error states are friendly and never leak stack traces (CSP/headers + generic 500s).
4. **Backups & recovery:** Supabase backup cadence confirmed; documented restore procedure; export of customs/config data.
5. **Runbooks:** incident response (the `engineering:incident-response` skill), on-call expectations even if just you, rollback procedure (Vercel preview → promote), and the deploy checklist (`engineering:deploy-checklist`).
6. **Load/cost ceiling:** rate limits + a hard monthly spend cap on the AI providers so a traffic spike or abuse can't produce a surprise bill.

### F. Quality, accessibility & content integrity

**Engineering status: ✅ done as scoped 2026-08-20 — see `claude_code_prompts.md` Prompt 18's status row, `docs/CONTENT_QA.md`, and `docs/ACCESSIBILITY_AUDIT.md` for the full evidence trail.** Coverage gate ratcheted 60%→85% against a measured ~91.4% actual; `docs/CONTENT_QA.md` (new) documents the customs-corpus pipeline and states plainly no rabbinic endorsement exists; automated WCAG2AA CI gate (`pa11y-ci`) added and verified 8/8 pages, 0 violations. **One real gap found and deliberately left open, catalogued at §26:** the new `pa11y-ci` gate only ever renders each page's light theme — dark theme is not covered by this automated gate (the existing manual `docs/ACCESSIBILITY_AUDIT.md` pass does cover dark tokens, by a different mechanism).

1. **Test coverage gate** (roadmap §7.2 — suite now exists): enforce coverage in CI; add the golden-master tests from the refactor phases.
2. **Accessibility to WCAG 2.1 AA** (per `ENGINEERING_RULES.md`): automated axe-core/pa11y in CI + manual audit; publish the accessibility statement (§A.7).
3. **Content QA for religious accuracy:** a documented review process / disclaimer acknowledging the app is not rabbinically supervised (or, if you obtain a rabbinic advisor/hechsher-equivalent endorsement, document its scope). State clearly which it is — overclaiming authority is itself a liability.
4. **Localization correctness:** Hebrew/RTL correctness (dynamic `lang`/`dir`, §7.10) and that translated content isn't presented as authoritative.

### G. Business & compliance scaffolding (discuss with attorney/accountant)

**Engineering status: documentation only 2026-08-20 — none of these 5 items are engineering-actionable.** `docs/LAUNCH_CHECKLIST.md`'s "Business & compliance — needs human counsel" section restates all 5 items in plain language for whoever picks them up; nothing below has moved from "not started."

1. **Entity & insurance:** operating through an LLC/corp (liability shield) and **tech E&O / general liability / cyber insurance** are the practical backstops behind the contractual disclaimers — arguably the single most effective "don't get sued into personal bankruptcy" step. Discuss with counsel.
2. **Trademark/name clearance** for "Sh'elah" and logo; domain/brand.
3. **Accessibility/consumer-protection posture** for your launch markets (US ADA, EU EAA 2025, etc.).
4. **AI-specific regimes:** EU AI Act transparency obligations (§A.3); any future US state AI disclosure laws — monitor.
5. **Payment/commerce:** if you ever monetize, that triggers a separate stack (refund policy, billing terms, PCI via the processor, tax) — out of scope until then, but flag it.

### H. Launch gate — definition of "production ready"

A single checklist that must be fully green before public launch. **`docs/LAUNCH_CHECKLIST.md` (new 2026-08-20) is now the live, evidence-linked tracker for these 9 lines** — it re-derives each line's status independently and cites the actual code/doc backing it; treat it as authoritative over the terse checkboxes below, which are kept here only as the original definition-of-done statement.

- [ ] ToS, Privacy Policy, AI Disclosure, AUP, DMCA, Accessibility, Licenses — drafted **and attorney-reviewed**, versioned, dated, notice given on every page. *(Drafted, versioned, dated, and cross-linked in a persistent footer notice 2026-08-16 — attorney review is the only remaining item on this line. Note: the footer is browsewrap notice, not clickwrap consent — see §8.A's "Consent mechanism update" note for the open question on whether clickwrap at sign-up should be added.)*
- [ ] Persistent AI answer disclaimer live; safety routing for medical/legal/abuse queries verified.
- [ ] Age gate (13+, EU 16+ or parental consent) enforced at sign-up + stored with consent; AGE_APPROPRIATE_DIRECTIVE + safety classifier + post-generation output check live and tested in en/he. *(**Wording corrected 2026-08-20 — found stale by `claude_code_prompts.md` Prompt 19.** The "enforced at sign-up + stored with consent" half of this line no longer describes what shipped: per the 2026-08-16 product decision recorded in §8.A's "Consent mechanism update," age notice is a non-blocking browsewrap footer with no checkbox, no DOB/attestation collection, and no persisted record — there is no age gate to enforce today, at sign-up or otherwise. The second half — AGE_APPROPRIATE_DIRECTIVE + safety classifier + post-generation output check — is shipped and tested per §B-AGE. This line stays unchecked until an actual age-collection mechanism exists; see `docs/LAUNCH_CHECKLIST.md` line 3 for the detailed evidence trail.)*
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

### 10.3 GitHub profile update — ✅ DONE 2026-08-21 (Prompt 23 closed; scope reduced to item 1)

**Status:** the profile README is live. `github.com/akivayevdayev-debug/akivayevdayev-debug` didn't exist yet (confirmed via `gh repo view`, 404), so it was created as a new public repo via `gh repo create ... --public --push` (the authenticated `gh` session in this environment is the operator's own `akivayevdayev-debug` account, token scopes `gist, read:org, repo, workflow`) with the operator's own approved README content — not the placeholder draft below, which used a different tagline/email and is left in place only as history. Confirmed rendering correctly via `gh repo view` after push.

Item 2 (pinning repos), item 3 (Sh'elah repo topics/description/website polish), and item 4 (profile bio/links) are closed, not done — the operator confirmed this prompt complete with only the README shipped. Reopen explicitly if any of these become wanted later.

**Original planning note (superseded by the above, kept for history):** the Chrome extension is **not currently connected** (no browser instance detected), so this can't be executed yet. To run it: install/enable the Claude-in-Chrome extension and sign in, then I can drive it. I'll also need your GitHub **username** confirmed (the profile README lives in a special repo `github.com/<username>/<username>`).

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

> Status: `api/index.py` created and `vercel.json` replaced with the modern `functions` + `rewrites` + `headers` form. JSON validated, entrypoint compiles, rewrite regex verified against static/app paths.
>
> **Update (2026-08-21):** the preview-deploy verification checklist below is now done — the operator ran a real `vercel deploy` and confirmed the live preview works (Prompt 24 closed). Getting there required fixing an unrelated local blocker first: the `vercel` command on the operator's machine was shadowed by an unrelated same-named pip package (`vercel` 0.5.8, "Python SDK for Vercel", a `vercel-workers` dependency) that looped infinitely instead of running the real CLI — fixed by installing the real CLI via `npm install -g vercel` (now resolves correctly). Separately, this line's own "rewrite regex verified" claim above does **not** match the live file re-checked the same day — `vercel.json` currently has **no `rewrites` key at all** (only `functions`/`regions`/`crons`/`headers`); see §29.2/§30 and Prompt 42/§29 STEP 2 for the open investigation into whether that's a real gap or the routing works for another reason. The completed preview-deploy pass did not specifically re-check this discrepancy item-by-item, so treat it as still open. Production promotion has not been requested and has not happened.

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

> **⚠️ Correction (2026-07-28, from §13 research):** the `"memory": 1024` key written into `functions` during the Option-A fix is **invalid under Fluid compute** — Vercel's docs state you cannot set memory in `vercel.json` and will emit a build-time warning if you try; on Hobby the size is fixed (Vercel-managed, ≥1 vCPU) and on Pro/Enterprise it is set in **Settings → Functions → Advanced**. The key is ignored, not fatal, but it must be removed. `maxDuration` **is** still valid in `vercel.json` and stays. See §14.2.1.

---

## 12. PHASE 6 — Product surface, content & growth layer (external UI review, verified against codebase)

**Provenance & verification discipline.** This phase originates from an external (Grok) review of the live site. That review was produced **without codebase access**, so every claim was verified against the repo before admission. Claims that were wrong or already shipped are recorded below and **not** re-planned; claims duplicating existing plan sections are cross-referenced, not duplicated. Only validated, net-new work becomes Phase 6 scope.

**Status (2026-08-21):** §12.1, §12.2, §12.3.1, §12.3.3, §12.3.4, §12.4, and §12.5.2-§12.5.4 shipped under Prompt 25 — full `pytest -q` green, verified against a real dev server. §12.3.2 (search filter UI) was deliberately descoped, not built. Three claims below (citation deep-links, JSON-LD, breadcrumb backend data) turned out to be already-shipped before Prompt 25 — see the corrected rows and §29.4. Seven follow-up items (one descope, one config contradiction, one content gap, three doc corrections, one counting quirk, one unrelated live bug) are tracked in §29 / `claude_code_prompts.md` Prompt 42.

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
| Citation links on AI sources | ❌ Wrong — already shipped | The AI-source-box's delegated click listener already called `readText(ref, {skipNavigationGrid:true})` on `a.source-local-link` clicks before Prompt 25 touched anything (confirmed 2026-08-21, §29.4). No code needed. |
| Disclaimers, guardrails, rabbinic consultation | ✅ Correct — **already planned** | §8.B / §8.B-AGE in full. No new scope. |
| WCAG, mobile, tap targets, contrast | ✅ Correct — **already planned** | §7, §8.F.2, `ENGINEERING_RULES.md`. No new scope. |
| User feedback mechanism for synthesis accuracy | ✅ **Correct — net-new** | No feedback capture exists anywhere in routes/UI. → §12.4 |
| SEO: meta, structured data, sitemap, keywords | ✅ Correct — net-new beyond §7.10 | §7.10 covers meta/OG only; no `sitemap.xml`/`robots.txt` existed. **Correction (2026-08-21, §29.4):** a `WebApplication` JSON-LD block already existed on the home page — the "no JSON-LD exists" premise was stale; §12.5.2's work was adding `WebSite`/`Organization` alongside it, not creating JSON-LD from scratch. → §12.5 |
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

1. **Breadcrumbs** in the reader/library view reflecting the Sefaria category tree (`get_library_index` already exposes the hierarchy) — server-provided data, client-rendered, ARIA `nav[aria-label="breadcrumb"]`. **Correction (2026-08-21, §29.4):** `backend/sefaria_library.py::get_text`/`_parse_v3_response` already returned a `categories` field on every `/api/text/<ref>` response before Prompt 25 — this shipped as a frontend-only change (an `nav[aria-label="breadcrumb"]` wrapper reusing the existing JS write-target), no backend work was needed.
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

1. **`robots.txt` + `sitemap.xml`**: `robots.txt` (allow public pages, disallow `/api/`, devtools); generated sitemap covering the stable public routes (home, about, help, glossary, legal pages, parasha page — *not* per-ref library URLs initially). **Correction (2026-08-21, §29.2/§29.3):** shipped as Flask routes in `backend/routes_pages.py`, matching the existing favicon.ico/manifest.webmanifest/service-worker.js pattern — not as physical static files, since `vercel.json` has no `rewrites` array to exclude them from the function (§11/§14.3.2's premise doesn't hold; see §29.2). The parasha page does not exist and is omitted from the sitemap (see §29.3) rather than pointing at the JSON-only `/api/parasha` endpoint.
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

The 5 low-risk `role="status"` → `<output>` renames were fixed in the same pass as §13's mechanical bucket. **Update 2026-08-16:** `#legalModal` no longer exists — it was replaced by `.site-footer`, a static, always-visible footer notice (not a dialog at all, so `Web:S6819` no longer applies to it). That leaves 2 real modals, not 3: `#calendarModal`, `#chapterGridModal` (`templates/index.html`) — still use `role="dialog"` + manual show/hide JS and are **deferred**:

1. Convert each to a native `<dialog>` element; replace the manual `display`/class-toggle open logic with `.showModal()` and close logic with `.close()`.
2. Native `<dialog>` provides browser-default focus-trap and `::backdrop` — audit existing custom backdrop-click-to-close and Escape-key handlers for double-handling once the native behavior is live (native `<dialog>` already closes on Escape; a duplicate handler would double-fire).
3. Re-test each modal's full interaction surface in-browser before merging: open trigger, close button, backdrop click, Escape key, focus returns to the trigger element on close, and screen-reader announcement on open (matches the WCAG dialog pattern this rule exists to enforce).
4. Ship as three independent small PRs/commits (one per modal), not one combined diff — each has a distinct trigger/close call-site surface and independent regression risk.

**Exit criteria for this section:** re-run SonarCloud after §4 lands to confirm the S3776 count has dropped as predicted before doing manual refactors on what's left; all 3 modals interaction-tested in-browser (not just visually) before each commit.

**Status (2026-08-22, Prompt 26): partially done — see §32 for the full list of gaps left for Opus 5.** No CI/SonarCloud credentials were available locally, so `radon` cyclomatic-complexity (`cc`) was used as an approximation for `python:S3776` — not equivalent (Sonar's cognitive-complexity metric weights nesting more heavily than radon's cyclomatic count), but sufficient to rank hotspots. §13.1: the predicted §4 side-effect reduction only partly materialized — `backend/utils/search_provider.py` still had one E-grade function (`_build_last_resort_web_sources`, radon E/37) after the §4 extraction, contradicting the "naturally drop below threshold" expectation (see §32.3). Per the plan's explicit "ONE AT A TIME... never a bulk pass" instruction, only the 3 worst hotspots were refactored this pass, each its own reviewed, test-anchored commit: `backend/utils/search_provider.py::_build_last_resort_web_sources` (E/37→B/10, commit `87968a2`), `backend/sefaria_library.py::_add_search_library_result` (E/33→B/10, commit `a2c7c2f`), `app.py::_coerce_ai_answer_shape` (D/23→B/7, commit `e6914cc`). ~47 lower-severity hotspots (mostly C-grade) remain — full radon-graded list is in §32.1, left as backlog rather than risking a bulk pass. §13.2: both `#calendarModal` and `#chapterGridModal` were **already** native `<dialog>` elements with `.showModal()`/`.close()` in the working tree before this session started (from an earlier, unfinished, uncommitted session — `#legalModal` was already gone per the 2026-08-16 update above). This session's contribution was the double-handling audit: found and removed a duplicate `closeChapterGrid()` call sitting in the global `keydown` Escape handler alongside `chapterGridModal`'s own native `cancel`-event listener (would double-fire on every Escape press) — fix verified in-browser (open/close/backdrop-click confirmed for both modals; the native `cancel`-event wiring verified correct via manual `Event('cancel')` dispatch, see §32.5 for a caveat on Escape-key verification specifically). **The fix could not be committed** — `templates/index.html` carries ~1,900 lines of unrelated pre-existing uncommitted changes (an analytics-consent-gate feature, JSON-LD blocks, an icon-import helper, the legalModal removal) that were never authored or reviewed in this session; the one-line fix sits inside a hunk that also contains all of that, so isolating it cleanly was not possible without reviewing the entire bundle first. See §32.2. `calendarModal` also lacks the backdrop-click-to-close handler `chapterGridModal` has (§32.4) — left alone as a possible intentional design asymmetry, not fixed.

---

## 14. PHASE 8 — Vercel Fluid-compute cost optimization (Active CPU · Provisioned Memory · Fast Origin Transfer · Invocations)

**Objective:** cut Vercel resource consumption as far as possible **without changing a single user-visible behavior**, and without disturbing the Git integration that already auto-deploys the newest commit. The governing principle the user stated — *"recall only when needed and for the rest of the time remain offline or inactive"* — is exactly how Fluid already bills; this phase's job is to remove everything in our code and config that defeats it.

### 14.1 How Vercel actually charges (researched 2026-07-28, Vercel docs)

Four independent meters, each with a different lever. Understanding which one dominates **for this app specifically** is the whole game:

| Meter | What it measures | Hobby allowance | Rate (iad1/pdx1/cle1 — the cheapest regions) |
|---|---|---|---|
| **Active CPU** | CPU-milliseconds your code *actually executes*. **Billing pauses during I/O** (DB queries, LLM calls, HTTP waits). | 4 CPU-hours/mo | $0.128 / CPU-hour |
| **Provisioned Memory** | Memory × **wall-clock instance lifetime**, in GB-hours. Bills **through I/O waits** and **continues until the last in-flight request completes**. | 360 GB-hours/mo | $0.0106 / GB-hour |
| **Invocations** | Every incoming request that reaches a function — success, error, or timeout. **Edge-cache hits are excluded.** | 1,000,000/mo | billed on Pro |
| **Fast Origin Transfer** | Bytes moved **between the CDN and the function**, both directions (request headers+body in, response out). **A CDN cache hit never contacts the origin → contributes 0.** Origin transfer is auto-compressed by Vercel. | plan-dependent | plan-dependent |

**Between requests, a paused instance costs nothing** — no CPU, no memory. Fluid already "remains offline when not needed." The corollary is the load-bearing rule of this phase:

> **Anything that invokes a function, or extends an instance's lifetime, when no user is waiting on it, is pure waste.** Cron jobs, keepalive pings, high-frequency uptime probes, background tasks that outlive the response, client-side polling, and crawler traffic on dynamic routes all fall under this.

**Which meter dominates Sh'elah.** `/ask` is I/O-bound: it spends most of its wall-clock time waiting on Gemini/Claude/Sefaria, not computing. So **Active CPU per ask is small; Provisioned Memory is the larger meter**, because it bills the full 5–30s wall clock at the instance's memory size. Two consequences that shape everything below:
- **Shortening wall-clock latency saves real money** (memory is billed per second of lifetime), whereas micro-optimizing pure-Python CPU mostly does not.
- **Fluid's optimized concurrency is a discount, not a cost**: multiple concurrent requests share one instance, so the same GB-hours cover more asks. Anything that forces instance *fan-out* (blocking the event loop so requests can't share, per `ENGINEERING_RULES.md`) silently multiplies memory cost. The existing "no blocking I/O on the FastAPI event loop" rule is therefore also a **billing** rule.
- **The single biggest lever is not in the function at all — it's the CDN.** A request served from edge cache costs **zero** on all four meters. §14.3 is where most of the savings live.

### 14.2 Configuration corrections (do first — cheap, zero risk)

**14.2.1 Fix `vercel.json` (supersedes part of §11).**
- **Remove `"memory": 1024`** — invalid under Fluid compute (build-time warning; ignored). Memory/CPU size is dashboard-only (Pro/Enterprise) and Vercel-managed on Hobby.
- **Keep `maxDuration`, and lower it deliberately.** Fluid's default max duration is **300s**. A hung or pathological request bills Provisioned Memory for its entire lifetime, so the ceiling *is* a cost control. Set it to just above the real p99 of `/ask` (`AI_TOTAL_BUDGET_SECONDS` + fallback headroom — the current `90` is already sane; confirm against observed p99 and tighten if the data supports it, never loosen without cause).
- **Pin the region** with `"regions": ["iad1"]` (or `pdx1`/`cle1`) — the three cheapest regions at $0.128/CPU-hr and $0.0106/GB-hr, versus e.g. $0.221/$0.0183 in `gru1`. Pick the one nearest the user base; a single region also avoids paying to warm several. Verify the choice doesn't add meaningful latency for the actual audience before committing.
- Do **not** add `"fluid": true` blindly — confirm the project's current Fluid state in the dashboard first (Fluid is default for projects created after 2025-04-23). Flipping it changes the execution model; it should be a deliberate, verified step, not a config-file side effect.

**14.2.2 Spend guardrails (ties §8.E.6).** Enable **Spend Management** with a hard cap and **Usage Notifications** thresholds in the Vercel dashboard, so a traffic spike, a crawler, or a runaway agent loop (§9) cannot produce a surprise bill. Document the configured values in `docs/RUNBOOKS.md`. This is a dashboard action, not code — but it is a launch-gate item (§8.H).

**14.2.3 Build-side waste.** Preview deployments consume build minutes and their own function resources. Two safe reductions: (a) an `ignoreCommand` (or `git.deploymentEnabled` scoping) that skips builds for doc-only commits touching just `*.md`/`docs/**` — **note the trade-off honestly: those commits then aren't deployed, which is correct only because they change nothing servable**; (b) keep noisy experimental branches out of auto-deploy. **Neither touches the production Git integration — the newest commit on the production branch still auto-deploys exactly as it does today.**

### 14.3 The CDN lever — cache what is cacheable (largest single win)

**Verified current state (`app.py::apply_response_cache_policy`, L1144–1205):** every path under `/api/` plus `/ask` and `/set_location` is served **`Cache-Control: no-store`**. That is correct and necessary for `/ask` (personalized, non-deterministic, auth-scoped) and for anything user-scoped — but it is applied indiscriminately, including to responses that are **immutable or slow-changing and identical for every user**:

- Sefaria text by ref, library index, category trees, commentary links — effectively immutable.
- Zmanim for a given (date, lat, lon, community) — deterministic; the answer for a past or present date never changes.
- Hebrew-date conversion, parasha, omer, holidays for a date range — deterministic calendar math.
- Community customs/profile documents — change only when the corpus is edited.
- Static-ish word/lexicon lookups.

Each of these currently costs a full function invocation + Active CPU + Provisioned Memory + Fast Origin Transfer **on every single request, from every user, forever**. Cached at the CDN, the *second and every subsequent* request costs **nothing on any meter**.

**14.3.1 Introduce a cache-policy tier table.** Replace the blanket `/api/` → `no-store` branch with an explicit, auditable classification. Every route lands in exactly one tier; the default for an unclassified route stays `no-store` (fail-safe: a new route is never accidentally made public-cacheable).

| Tier | Applies to | Header |
|---|---|---|
| **Immutable** | text-by-ref, library index/categories, commentary links | `public, s-maxage=86400, stale-while-revalidate=604800` |
| **Deterministic-by-date** | zmanim, hebrew-date, parasha, omer, holidays for a *specific* date/location | `public, s-maxage=3600, stale-while-revalidate=86400` (past dates may use the Immutable tier) |
| **Corpus-derived** | customs, community profiles, glossary | `public, s-maxage=3600, stale-while-revalidate=86400` |
| **Private / never cache** | `/ask`, auth'd user data (bookmarks, history, preferences), devtools, feedback POST, anything reading a session/JWT | `private, no-store` (unchanged) |

Rules that make this safe:
- Use **`s-maxage`** (shared/CDN cache) and keep browser `max-age` short or zero — the CDN absorbs the load without freezing content in users' browsers.
- **`stale-while-revalidate`** lets the CDN serve instantly while refreshing in the background: user-visible behavior is *faster*, never staler than the SWR window.
- Any response that varies by user, auth state, or language **must** carry the correct `Vary` header (or the variant must be in the URL). Getting this wrong leaks one user's data to another — treat it as a security review item (§8.C), not a performance tweak. Safest form: keep every cacheable route **auth-independent by construction** and never cache a response produced while a user token was present.
- Corpus edits need an invalidation story: version the cache key (e.g. include a corpus/deploy hash — `DEPLOY_HASH` already exists) so a redeploy naturally busts stale entries.

**14.3.2 Verify static assets never touch the function.** The §11 rewrite already excludes `static/`, `favicon.ico`, `manifest.webmanifest`, `service-worker.js`. Confirm on a preview deploy that these are edge-served (response headers show no function involvement) — a static asset accidentally routed through the Python function pays all four meters for a file the CDN could serve free. Extend the negative-lookahead when §12.5 adds `robots.txt`/`sitemap.xml`.

**14.3.3 Client-side invocation audit.** Every fetch the frontend makes is an invocation. Audit for: polling loops, timers that re-fetch on an interval, duplicate fetches on page load, prefetch-everything patterns, and service-worker revalidation storms. Replace polling with on-demand fetching; debounce search-as-you-type (each keystroke firing a request is 10× the invocations for one user action); let the service worker serve cached shells rather than re-hitting the origin. Pair with §12.5's `robots.txt` so crawlers stay off dynamic routes entirely.

### 14.4 Active-CPU reduction (cold-start and hot-path work)

**14.4.1 Import-time work is charged on every cold start.** Verified: `app.py` calls `validate_all_customs_at_startup()` at module import, and imports a wide dependency surface. Under Fluid, instances are reused — but every *new* instance pays this in Active CPU before serving its first byte, and it also lengthens that first request's wall clock (→ memory).
- Make corpus validation **lazy or dev/CI-only** (it is a developer safety net, not a production request-path need): run it in CI and behind an env flag locally, skip it in production imports. Zero behavior change for users; strictly less startup CPU.
- Audit remaining module-level work: file reads, JSON parsing, regex compilation of large tables, client construction. Regex compilation is cheap and worth keeping hoisted; **large JSON corpus loads should become lazy singletons** (loaded on first actual use, then cached for the instance's life — which Fluid makes long-lived and therefore effective).
- Defer heavy optional imports into the functions that need them, so a request that never touches a subsystem never pays to import it.

**14.4.2 Don't do work the CDN already did.** Every response promoted to a cacheable tier in §14.3 eliminates its CPU entirely on cache hits — this is why §14.3 precedes CPU micro-optimization.

**14.4.3 Protect Fluid's concurrency discount.** Re-audit that no blocking call sits on the FastAPI event loop (`asyncio.to_thread` / async httpx only, per `ENGINEERING_RULES.md`). Blocking the loop prevents in-instance concurrency, forcing Vercel to spin up additional instances — each with its own Provisioned Memory bill for the same traffic. Phase 5's loop-bridge (§4 Phase 5.1) matters here too: the bridge is an escape hatch, and every trip through it is an extra thread and extra wall-clock — the warning it logs should be treated as a cost signal, not just a correctness signal.

**14.4.4 Shorten wall clock on `/ask`.** Because memory bills for the whole lifetime, latency reduction *is* cost reduction. Levers that already exist or are planned: parallel fan-out (already done via `asyncio.gather`), circuit breakers skipping known-dead providers instead of waiting for their timeouts (~~⚠️ corrected 2026-08-19: NOT implemented for the AI providers~~ **✅ re-corrected 2026-08-20 — now shipped.** Prompt 39/§26.1 wired `health.is_healthy`/`record_success`/`record_failure` into all three `/ask` AI-provider entry points in `backend/claude.py` (`_call_gemini_model` sync primary, `_call_anthropic_httpx_model` Claude fallback, `_call_gemini_httpx_model` async primary) — a consecutively-failing provider now short-circuits instead of paying its full timeout on every call. See §24.2/§24.5, both similarly stale and corrected in place), tight per-provider timeouts, and the `TTLCache`/instance-local caches avoiding repeat upstream calls within an instance's life. **Do not** shorten latency by removing the fallback ladder — correctness and the fail-open guarantee outrank cost.

### 14.5 Fast Origin Transfer reduction

FOT counts request bytes in **and** response bytes out, and is zero on CDN hits — so §14.3 is again the primary lever. Beyond it:

1. **Trim response payloads.** Audit the `/ask` JSON: `sources` carries full `lines` arrays. `_compact_ai_sources` already exists to condense them — verify it is applied on every return path (including both fallback branches in `asgi.py`) and that no debug/echo field ships to production. Drop or gate any field the UI does not render.
2. **Don't round-trip large bodies.** Requests carrying big payloads to functions count on the inbound side; keep request bodies small (already capped by the payload limits in §8.C.5).
3. **Vercel auto-compresses origin transfer** — no action needed, but do confirm application-level responses aren't double-compressed or, worse, sent uncompressed with an incorrect `Content-Encoding`.
4. **Never serve binaries/static through the function** (§14.3.2). Exports (docx/pdf) are the exception where the function must produce bytes — those are user-initiated and infrequent; keep them out of any prefetch path.

### 14.6 Invocation reduction & the "stay inactive" rule

1. **No warmers, keepalive pings, or scheduled requests whose purpose is keeping an instance alive.** Fluid pauses instances for free; a warmer converts free idle into billed idle. **Amended 2026-08-19 (§24.3):** this previously read "No cron jobs" with no exception, and there are now two live cron entries — `/api/devtools/budget-check` (13:00 UTC) and `/api/devtools/retention-enforce` (14:00 UTC). They do not violate the rule's intent: both are once-daily admin jobs doing real, non-request-path work, one of which (retention) is a legal requirement per §8.D.5. **Scheduled jobs performing real non-request-path work are permitted, must be justified in this document, and must be counted against §14.6's invocation budget.** Anything whose only effect is keeping an instance warm remains forbidden. Mirror this wording into `.agents/ENGINEERING_RULES.md` (§14.7.5 carries the un-amended version). Note `budget-check` is presently a **no-op** — `DAILY_BUDGET_USD` is unset — costing an invocation a day for nothing; configure or disable it (§24.3, §24.4).
2. **Reconcile with §8.E.2 (uptime monitoring) — an actual conflict, resolved here.** An external monitor hitting `/api/health` every 30s is **2,880 invocations/day (~86k/month)** of pure cost with no user value, and each one wakes an instance. Resolution: (a) poll at **5-minute** intervals, not 30-second, for a solo-operated project; (b) point the primary uptime check at a **static** asset (edge-served, zero function cost) to detect total outages, and use the function-backed `/api/health` as a **secondary, low-frequency** deep check; (c) keep `/api/health` genuinely cheap — no upstream fan-out, no DB round-trip on the default path (gate deep checks behind an explicit query flag). Record the chosen intervals in `docs/RUNBOOKS.md`.
3. **Background work must not outlive the response.** Any post-response task (analytics, logging, memory-summary writes) extends the instance lifetime and therefore Provisioned Memory. Prefer doing the work inside the request when it is small, batching it, or moving it off-platform. Audit `_store_user_memory_summary` / `_store_ask_history` — they are currently awaited inside the request (correct for billing); do not "optimize" them into fire-and-forget background tasks without accounting for the memory-billing tail.
4. **Errors and timeouts bill too** — invocations count regardless of outcome. A route erroring in a retry loop is billed on every attempt; make sure client-side retry has backoff and a ceiling.

### 14.7 Measurement, verification & guardrails

1. **Baseline before changing anything.** Record 30 days of Usage-dashboard figures per meter, split by project and region (Usage → Last 30 days → by Project/Region), plus the Requests **cached-vs-uncached ratio**. Without a baseline no claim of improvement is checkable.
2. **Primary success metric:** the cached-request ratio should rise substantially after §14.3, with Invocations, Active CPU, Provisioned Memory, and Fast Origin Transfer all falling. Re-measure 7 and 30 days after.
3. **Zero-behavior-change proof (non-negotiable):** the full offline suite stays green; add tests asserting **exact `Cache-Control` values per route tier** (this is the regression net that stops a future edit from silently making a private route publicly cacheable) and asserting authenticated/user-scoped routes are **never** `public`.
4. **Preview-deploy verification** (extends §11's checklist): confirm cacheable routes return the expected headers and show CDN `HIT` on a second request; confirm `/ask` and every authed route still show `no-store`; confirm static assets are edge-served; confirm cold start still succeeds; confirm no cross-user leakage on any newly cached route (fetch as user A, then as user B, compare).
5. **Add to `.agents/ENGINEERING_RULES.md`:** *"Every new route declares its cache tier explicitly; unclassified defaults to `no-store`. Nothing may be made `public`-cacheable if its response varies by user, auth state, or session. No cron jobs, warmers, or keepalive pings. Post-response background work extends Provisioned Memory billing — justify it or do it in-request."*
6. **Documentation:** `docs/VERCEL_COST_OPTIMIZATION.md` — the meter model, the tier table, the measured baseline and post-change numbers, the monitoring-interval decision, and the spend-cap values.

### 14.8 Status (2026-08-22, Prompt 27 — §14.2, §14.4.1 quick wins only)

**14.2.1 done, commit `06ae20f`.** The uncommitted working-tree `vercel.json` was found to have *reintroduced* the exact routing regression fixed by commit `7c177b3`: a `"rewrites"` block with `"destination": "/api/index"` (a literal string, no `$1` capture-group forwarding), which makes Vercel pass that literal path to the app instead of the real request path. Fixed by removing the `rewrites` block entirely (matching `7c177b3`'s original fix) while keeping the rest of the uncommitted diff's legitimate changes: `"memory": 1024` removed, `"regions": ["iad1"]` added, `maxDuration: 90` kept (already justified against `/ask`'s observed p99 + fallback headroom, unchanged from its prior value), and `"ignoreCommand"` added per §14.2.3 (see below). `"fluid": true` was **not** added — no dashboard check was performed this pass, per the plan's explicit instruction not to add it blindly.

**14.2.3 done, same commit.** `ignoreCommand` set to `git diff --quiet HEAD^ HEAD -- . ':(exclude)*.md' ':(exclude)docs/**'` — exits 0 (skip build) when the commit touches only markdown/docs, non-zero (proceed) otherwise. Trade-off stated in the commit message: a commit that touches only excluded paths never deploys, which is correct only because those paths change nothing servable. Production Git integration otherwise untouched — newest-commit auto-deploy on non-doc commits is unaffected.

**14.2.2 not performed, per the prompt's explicit "report, do not perform" instruction.** `docs/RUNBOOKS.md` was read and found to already substantively satisfy this item from earlier work (445 lines, concrete 50%/80% Usage Notification thresholds, a formula-based Spend Management cap pending the not-yet-captured 30-day baseline from §14.7.1) — no further edit was made this pass since the existing content already matches what this item asks be recorded.

**14.4.1 partially done, commit `de819c1`.** Most of this item was **already implemented** in the uncommitted working tree before this session started (likely from the same earlier unfinished session that left `templates/index.html` and other files dirty) — `validate_all_customs_at_startup()` was already gated, and large corpus JSON loads were already lazy singletons. The one genuinely missing piece — deferring the `anthropic`/`google-genai` SDK imports in `backend/claude.py` from module load to first use — was implemented this pass: both were previously eager top-level imports (`try: import anthropic ...` / `try: from google import genai ...`), now replaced with `_ensure_anthropic_loaded()`/`_ensure_genai_loaded()` lazy loaders called from `_get_client()`, `_get_async_client()`, and `_configure_gemini_client()`. `app.py`'s module-level `from supabase import create_client` was identified as the same class of gap but **deliberately not touched** — it has a much larger blast radius (referenced across many route files) than `backend/claude.py`'s ~3 contained call sites, and fixing it safely needs its own reviewed pass. See §32.6.

**Verification:** `python3 -c "import app, asgi"` succeeds; full `pytest -q` green (all tests passing, no failures) after all 5 commits this pass; `vercel.json` parses via `python3 -m json.tool` and matches the Option-A shape minus `memory`. `graphify update .` run. ~~Prompt 28 (cache-tier work) was **not** started, per the prompt's explicit stop instruction.~~ **Prompt 28 shipped 2026-08-22 — see §14.10.**

### 14.10 Status (2026-08-22, Prompt 28 — §14.3 cache tiers, §14.5 FOT, §14.6 invocation reduction, §14.7 measurement/docs)

**§14.3.1 done.** New `backend/cache_policy.py` is the single `classify_cache_tier(method, path)` source of truth for all four tiers, replacing the blanket `no-store` branch. Consulted from both `app.py`'s Flask `after_request` hook (WSGI-mounted routes) and — a gap the original audit missed — `asgi.py`'s `request_id_middleware`, since the two *native* FastAPI routes (`POST /ask`, `GET /api/async/health`) never reach `app.py`'s hook at all and previously shipped with **no** `Cache-Control` header whatsoever. Regression-tested (`tests/test_cache_policy.py::test_native_route_previously_had_no_cache_control_now_gets_private`).

**The cross-user cache-leak §14.3.1's own "Vary" warning anticipated, found for real.** `GET /api/zmanim`, `/api/zmanim/month`, and the `/api/holidays` fallback branch read `session['lat']`/`session['lon']` when the URL omits `lat`/`lon` — those specific responses are not a pure function of the URL. Fixed with a request-scoped escape hatch (`flask.g.cache_tier_force_private = True`, set at the three call sites in `backend/routes_calendar.py` before `apply_response_cache_policy()` consults the tier table) rather than pulling those routes out of the public tier entirely, since the common case (explicit `lat`/`lon`) is genuinely cacheable.

**§14.3.2 (static assets) not re-verified — deploy-only, see §33.4.** **§14.3.3 (client-side polling audit) done** — swept every `fetch`/`setInterval` in `static/js/*.js` and `templates/index.html`; the one polling interval found (`openDevtoolsInspector()`'s 15s heartbeat) is correctly gated behind explicit devtools-panel opening and cleared on close. No fix needed.

**§14.5 (FOT) verified, no new code needed.** `_compact_ai_sources` already runs on every `/ask` return path in `asgi.py`, now including Prompt 29b's new breaker-paused path (§16.10).

**§14.6 done.** No warmers found (§14.3.3's audit covers it, now codified as a standing rule in `.agents/ENGINEERING_RULES.md`). `/api/stack/health` re-verified cheap by reading `stack_health()` directly (no outbound fan-out, auth-gated). Background work (`_store_user_memory_summary`/`_store_ask_history`) confirmed still correctly in-request-awaited, not converted to fire-and-forget.

**§14.7 partial — the zero-behavior-change proof is done, the measurement is not.** `tests/test_cache_policy.py` (38 tests) pins exact `Cache-Control` values per tier and proves no user-scoped route is ever `public`; `.agents/ENGINEERING_RULES.md` gained the "Cost & caching" section §14.7.5 asked for; `docs/VERCEL_COST_OPTIMIZATION.md` is the §14.7.6 deliverable. **§14.7.1's baseline capture and §14.7.4's preview-deploy verification are explicitly not done** — both need a live Vercel environment this session did not have. See §33.4 for the itemized follow-up.

**Verification:** full `pytest -q` green (91.09% coverage, up from 90.39% at session start — 61 new/modified tests across `test_cost_meter.py`, `test_cache_policy.py`, `test_ask.py`, `test_rate_limit.py`); `graphify update .` run.

### 14.8 Explicitly out of scope / rejected

- **Scale-to-One / pre-warming:** keeps an instance alive to kill cold starts — the *opposite* of the "stay inactive" goal, and it bills Provisioned Memory continuously. Rejected for this project.
- **Raising memory to "go faster":** larger memory does raise available CPU (which can shorten CPU-bound work), but Sh'elah's hot path is I/O-bound, so it would raise the GB-hour rate on a meter that already dominates, for little CPU gain. Not on Hobby in any case (Vercel-managed).
- **Moving off Vercel / self-hosting:** out of scope; the Git-push-to-deploy workflow is a requirement.
- **Aggressive caching of `/ask`:** rejected. Answers are personalized (community lens, language, mode, user memory) and non-deterministic; caching them risks serving one user's answer to another. `/ask` stays `no-store`, permanently.

### 14.9 Exit criteria

- `vercel.json` corrected (no `memory`, deliberate `maxDuration`, pinned cheap region), validated, preview-deployed.
- Cache-tier table implemented, every route classified, per-tier header tests passing, no user-scoped route publicly cacheable.
- Import-time validation made lazy/dev-only; module-level heavy loads lazy.
- Client-side polling/duplicate-fetch audit complete; monitoring intervals reconciled and documented.
- Spend cap + usage alerts configured; baseline and post-change usage figures recorded in `docs/VERCEL_COST_OPTIMIZATION.md`.
- Full suite green; `graphify update .`; **zero user-visible behavior change** demonstrated on a preview deploy before promotion.

**Sequencing:** §14.2 (config) and §14.4.1 (lazy startup validation) are independent quick wins and can run immediately. §14.3 (cache tiers) is the main body of work and should follow §4 Phase 4 so route ownership is settled; it pairs naturally with §12.5's `robots.txt`/sitemap work. §14.7's measurement steps bracket everything.

---

**Awaiting your command to begin Phase 1.** No code will be written until then. The §8 legal documents will be drafted for **attorney review** — they are not a substitute for a lawyer. The §10.3 GitHub profile update needs the Chrome extension connected + your username before I can execute it. **§11 is a live deploy blocker — fixing `vercel.json` can be done independently and first if you want the site deploying again before the refactor begins.**

---

## 15. UI/UX bug-fix pass (2026-07-31) — ad hoc, not part of the numbered-prompt track

A user-reported punch list of 8 concrete bugs/regressions, investigated end-to-end (parallel investigation fan-out, each finding verified live against the running app before any fix) and fixed in the same session. Not part of `claude_code_prompts.md`'s numbered-Prompt track — recorded here for continuity since it touched many of the same files §4/§7 will eventually revisit.

1. **Full-article source-attribution mismatch** — `viewFullArticle()`/`renderArticle()` (`templates/index.html`) rebuilt the sources list from raw `data.sources` with no ranking/dedup/AI-cited-first logic, unlike the AI answer box (`populateAiModal`) — could surface a source the answer box had correctly excluded as irrelevant. Fixed by having the reader mirror the answer box's already-computed `#aiSources` HTML (`mirrorAiSourcesInto()`) instead of re-deriving it.
2. **Dark-mode color inconsistencies** — EN/HE toggle read as yellow (`--accent-gold-light` too saturated for the dark palette; switched to `--accent-gold`); calendar events were flattened to one gray in dark mode by a blanket `!important` rule fighting FullCalendar's inline per-event color, and the dead `.fc-event.holiday`/`.shabbat` selectors never matched any DOM element because JS never set those classes — fixed by wiring `category` into FullCalendar's `extendedProps` (a bare top-level field is silently dropped, not auto-promoted) and driving color entirely through new `.fc-event.cal-cat-*` classes tokenized in `tokens.css` (also corrected two stale/mismatched swatch values — shabbat and rosh-chodesh — that didn't match the backend's actual palette even before this pass); `#settingsPanel` buttons had no dark-mode override at all; text-selection highlight was scoped to `#reader` only, so the rest of the site fell back to the browser's default blue — widened to a site-wide `::selection` rule.
3. **Logo/header layout** — mobile grid still reserved a column for `#mobileNavBtn`, which had been deleted from the DOM, unbalancing the centering math and shifting the brand left of true center; switched to a 2-column left-aligned layout. Also unified title/subtitle line-height and let Tailwind's responsive `text-sm`/`sm:text-base` actually take effect (a stray `font-size: 1rem !important`-equivalent rule had been silently overriding it).
4. **Squished mobile calendar** — `.fc-event` had no `overflow`/`text-overflow`, so long holiday titles hard-truncated mid-word with no ellipsis inside a ~39px pill on a 375px viewport; added ellipsis handling, tightened mobile font/padding further, reclaimed modal padding, and added `dayMaxEvents: true`.
5. **Library sidebar dropdown arrows** — only the "Tanakh" summary had a styled chevron; every other `<summary>` in the same accordion either showed the browser's native (unstyled) triangle or no icon at all. Replaced with one global `::after`-based chevron rule scoped to `#leftSidebar`, so no per-summary HTML edits were needed across the ~15 call sites.
6. **Library loading reliability** ("random pauses, unexpected loading overlays, prayer books not loading at all, up-to-a-minute loads") — multiple independent causes, all fixed: a stray `chapterGridModal` (z-index 110) could be left open on top of the AI-answer modal (z-index 100) with no exclusivity logic, silently blocking it; `hideLoadingOverlay()`'s hide-timer was never cancelled, so a show/hide within 220ms could race and force the overlay closed mid-load; `readText()`/`openPrayerEntry()` ran a pre-flight network fetch *before* showing any loading feedback; `readText()` painted a blank reader pane while an unrelated prev/next-refs fetch blocked the actual content render (moved to fire-and-forget after first paint); no request-generation guard existed, so a slow response could overwrite a faster, newer one; `displayPrayer()` treated the static-preview endpoint's 404 as fully fatal even when the Sefaria full-text endpoint had succeeded. On the backend: `/api/siddur/full/<name>` fetched up to 80 refs fully sequentially (parallelized to a bounded thread pool); `get_index_leaf_refs` had no title-normalization/canonical-name fallback when Sefaria's index lookup missed, which is why some prayer titles returned nothing at all.
7. **Shulchan Arukh (and other halakhic works) chapter grid** — already routed through the same numeric grid function Talmud uses, but capped at 180/260 simanim (Orach Chayim has 697) and never grouped into named siman ranges, because the section-extraction helper only read Talmud's `alts.Chapters`, not the `alts.Topic` structure halakhic works actually use. Generalized the backend extractor to parse both shapes (`{label,heLabel,fromSection,toSection}` for the numeric case) and generalized the frontend's grouping gate from "looks like Talmud" to "section data exists" — Orach Chayim's grid now shows all 697 simanim across 29 named sections (e.g. "Laws of Tzitzit (8–24)").
8. **Phase 4b re-verification + doc accuracy** — confirmed the loop-bridge executor and `sefaria_library.py` locks are genuinely in place and passing (11/11, not the previously-claimed 15/15 — a miscount, corrected); found and corrected three other stale status rows in `claude_code_prompts.md` (§8.B AI safety and §8.B-AGE were marked "Not started" despite being fully implemented and tested in an earlier session; the Vercel `vercel.json` warning was already resolved).

All backend changes covered by the existing pytest suite (green, no regressions). Frontend changes verified live in-browser (both themes, desktop + 375px mobile viewport) rather than assumed from the diff.

---

## 16. PHASE 9 — Abuse resistance: rate limiting, availability protection & cost circuit-breaking

**Status: ✅ Phase 9a shipped 2026-08-22 — D1/D2/D4 fixed, D3 closed for any deployment with `RATE_LIMIT_REDIS_URL` set (open by default in local dev, loud warning on boot).** The two-limiter divergent-duplication defect (§16.8.1) is resolved: Flask-Limiter and asgi.py's independent in-process limiter are both removed, replaced by one Starlette middleware. **See §16.9 before acting on anything in §16.1–16.8** — those sections describe the pre-unification state and are kept for lineage, not as the current picture. Phase 9b (edge WAF + cost breaker) and Phase 9c (identity-aware quotas, Turnstile) remain unstarted and still block on §20a per §16.7.

> ⚠️ **Read §16.9 first**, then §16.8 for the defect-by-defect history. §16.1's four-defect analysis is dated 2026-07-31 and every defect it describes has since changed state — most recently, D1/D3/D4 closed for real and the §16.8.1 duplication finding was resolved by unification (2026-08-22), not by the rejected two-limiter route §16.8 verified was in place as of 2026-08-19.

Written 2026-07-31. Every finding below was read out of the repo at the cited line, not inferred from a review. Every external claim is cited to Vercel's own docs, dated.

### 16.0 The one-sentence diagnosis

**Sh'elah has the appearance of rate limiting and effectively none of the substance.** There are four independent defects, and *any one of them alone* is sufficient to reduce the limiter to decoration. All four are live in production simultaneously.

### 16.1 The four defects

#### D1 — The limiter is bolted to the wrong HTTP stack (fatal, production-only)

| | |
|---|---|
| **Where** | `asgi.py:187` vs `app.py:1372–1375` |
| **Severity** | Critical |

`asgi.py:187` registers `@fastapi_app.post("/ask")`. `asgi.py:556` then mounts the Flask app at `/` via `WSGIMiddleware`. Starlette walks its route table in registration order and the explicit `APIRoute` is registered ~370 lines *before* the catch-all `Mount`. Production `POST /ask` therefore terminates in `ask_async()` and **never reaches** Flask's `ask_question()` — which is the only function in the entire codebase carrying `@maybe_limit(...)` (`app.py:1374`; grep confirms exactly one call site).

The `429: {"description": "Rate limit exceeded..."}` entry in that route's `responses={}` dict (`asgi.py:189`) is **OpenAPI documentation. It enforces nothing.** It is actively harmful because it makes the endpoint *look* protected in `docs/API.md` and in the generated schema.

Net effect: **the single most expensive endpoint in the application — the one that spends real money on Gemini/Anthropic tokens on every call — is completely unlimited in production.**

Why this was never noticed: `python3 app.py` (`app.py:1921`) runs bare Flask with no ASGI layer, so the limiter *does* fire locally. It works everywhere except the one place that matters.

> **Verification requirement:** do not take the above on faith. Phase 9a must open with a characterization test that asserts, against the real `asgi.app`, that N+1 rapid `POST /ask` calls currently return `200` rather than `429`. Fix only after the test proves the bug (project rule: golden-master before change).

#### D2 — The rate-limit key is attacker-controlled

| | |
|---|---|
| **Where** | `app.py:816–820` (`_rate_limit_key`), `app.py:1228–1240` (`_extract_client_ip`) |
| **Severity** | Critical |

```python
forwarded = (request.headers.get("CF-Connecting-IP")
             or request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
```

`CF-Connecting-IP` is read **first**. shelah.org is served Vercel-direct; there is no Cloudflare in front of it. Vercel's request-headers reference (last updated 2025-12-13) enumerates the headers the platform sets and normalizes — `x-forwarded-for`, `x-vercel-forwarded-for`, `x-real-ip` — and states plainly that Vercel *overwrites* `X-Forwarded-For` and does not forward externally-supplied values, "to prevent IP spoofing." `CF-Connecting-IP` is **not** in that set, so it arrives verbatim from the client.

Consequence: `curl -H 'CF-Connecting-IP: <random>'` mints a brand-new rate-limit bucket on every single request. **Even after D1, D3 and D4 are all fixed, the limiter remains trivially bypassable while this line stands.** The same defect in `_extract_client_ip()` also poisons abuse forensics — the IP recorded next to a malicious `/api/client-errors` payload is whatever the attacker typed.

**Fix:** delete the `CF-Connecting-IP` branch entirely. Trust order becomes `x-vercel-forwarded-for` → `x-forwarded-for` → `x-real-ip` → `request.remote_addr`, and nothing else, ever. If Cloudflare is ever put in front of the site, that is a deliberate config change with a *trusted-proxy allowlist*, not a header read.

#### D3 — Counters are per-process and reset constantly

`RATELIMIT_STORAGE_URI` defaults to `memory://` (`app.py:812–813`). Vercel Fluid compute runs multiple concurrent instances and recycles them freely. In-memory counters are therefore per-process, unshared, and zeroed on every cold start and every deploy. The effective ceiling is `configured_limit × live_instances`, with an unbounded reset button attached. Flask-Limiter's own documentation warns against in-memory storage in production for exactly this reason.

#### D4 — Every route except `/ask` has no limit at all

`RATE_LIMIT_DEFAULT` parses to `[]` when the env var is unset (`app.py:806–810`), and `default_limits` is only passed to the `Limiter` constructor when that list is non-empty (`app.py:830–831`). Unless someone remembered to set an undocumented env var, **~45 API routes carry no rate limit whatsoever** — including the amplification surface below.

### 16.2 The amplification surface (cheap for the attacker, expensive for Akiva)

Ranked by damage-per-request. All are currently unlimited.

| Endpoint | Amplification | Meter hit (§14) |
|---|---|---|
| `POST /ask` (via `asgi.py:187`) | 1 request → LLM tokens + RAG fan-out + Sefaria calls | **Real USD** + Provisioned Memory |
| `GET /api/siddur/full/<name>` | 1 request → up to **80** Sefaria refs | Provisioned Memory + Fast Origin Transfer |
| `POST /api/export/chapter` (`routes_library.py:420`) | 1 request → reportlab/python-docx render | **Active CPU** — the one meter Sh'elah normally barely touches |
| `GET /api/library/search`, `/api/text/<ref>/links`, `/api/word/meaning`, `/api/geocode` | 1 request → N upstream calls | Provisioned Memory + FOT |
| `POST /api/client-errors` (`routes_devtools.py:135`) | **Unauthenticated**, no limit, accepts an 8 KB `stack` and forwards it to Sentry | Drains the free Sentry quota; poisons the error signal you rely on during an actual incident |

Also missing: **no `MAX_CONTENT_LENGTH` anywhere in the app.** An unbounded POST body reaches `/ask` and flows into token counting.

### 16.3 The architecture — three layers, one position

This section takes a position rather than listing options, because two of the plausible options are actively wrong for this codebase.

> **❌ Rejected: add `slowapi` to FastAPI and keep Flask-Limiter on Flask.**
> That yields two limiters, two stores, two key functions, two 429 body shapes and two config surfaces which *will* drift apart. That is precisely the "live, divergent duplication" failure mode §2 identifies as this project's most dangerous anti-pattern, re-created deliberately in the security layer. Not acceptable.
>
> **❌ Rejected: app-layer limiting alone.**
> Every 429 returned by application code still costs a function invocation and a full Provisioned-Memory wall-clock slice. Refusing an attack in Python means *paying, per request, to say no*. Under sustained flood that is indistinguishable from serving the traffic, on the meters that bill.

**L1 — Vercel WAF (edge). The only layer that makes an attack free.**

Per Vercel's WAF rate-limiting docs (last updated 2026-06-16) and the 2025-05-23 changelog, WAF-mitigated traffic incurs **no CDN request, no Fast Data Transfer, and no function invocation**. Hobby allocation, verified:

- **1 rate-limit rule per project**, 3 total custom firewall rules
- Counting keys: **IP and JA4 digest** (JA4 is a TLS-stack fingerprint — it survives IP rotation from a single toolchain, which is what an actual flood looks like)
- Fixed-window algorithm; window **10 s – 10 min**; **1,000,000 allowed requests/month included**
- **Persistent actions**: auto-block a repeat offender for 1–60 minutes so subsequent requests are dropped even earlier in the request lifecycle
- ⚠️ Counters are tracked **per region** — normally a real weakness, but `vercel.json` already pins `"regions": ["iad1"]`, so for Sh'elah specifically there is exactly one counter. This is a genuine synergy with §14, not a coincidence to leave undocumented.

Rule budget allocation (spend it deliberately — there are only four slots):

1. **Rate-limit rule (the one slot):** `POST /ask`, keyed on IP + JA4, fixed window 60 s, conservative limit, action = rate limit with a persistent block on repeat offenders. Deploy in **Log** mode first, read one week of real traffic in the Firewall overview, *then* switch to enforce. Do not guess the threshold.
2. **Custom rule 1:** deny common scanner paths (`/.env`, `/.git/*`, `/wp-admin/*`, `/vendor/*`, `/phpmyadmin/*`). These are pure noise today and every one of them currently costs a function invocation.
3. **Custom rule 2:** deny non-`GET`/`POST`/`HEAD`/`OPTIONS` methods site-wide.
4. **Custom rule 3: leave empty.** This is the incident-response slot. Burning all four in peacetime means having nothing to reach for at 2 a.m.

**L2 — One ASGI middleware in `asgi.py`, above both the FastAPI routes and the Flask mount.**

This is the only point in the process that observes 100 % of traffic exactly once. Everything lives here: one policy table, one store, one key function, one 429 body, one `Retry-After` header. **Flask-Limiter is removed from `app.py`, not left in place as a second opinion.**

- **Store:** Upstash Redis over `rediss://` (TLS Redis protocol; no connection pool to leak across Fluid instances; free tier is ample at Sh'elah's volume). One shared counter across every instance and every cold start — the direct fix for D3.
- **Failure posture — deliberately asymmetric.** If the store is unreachable: **fail *open* for library/text/calendar routes** (a reader looking up a Gemara should not be blocked because Redis blipped) and **fail *closed* for `/ask`** (an unmetered endpoint that spends money is a budget hole, not a degraded feature). Both paths alarm to Sentry. This asymmetry is the whole point and must not be "simplified" later.
- **Policy table**, keyed by route class, not scattered decorators:

| Class | Routes | Anonymous | Authenticated |
|---|---|---|---|
| `llm` | `POST /ask` | tight (per-min **and** per-day) | higher per-min, explicit daily quota |
| `heavy` | `/api/export/chapter`, `/api/siddur/full/*` | tight | moderate |
| `fanout` | `/api/library/search`, `/api/text/*`, `/api/word/meaning`, `/api/geocode` | moderate | generous |
| `telemetry` | `/api/client-errors` | tightest + same-origin required + body cap | same |
| `cheap` | static, health, calendar math | generous default | generous default |

  Every route falls into a class; there is no unlisted-therefore-unlimited path. **D4 is fixed by construction, not by remembering to add a decorator.**
- **Identity-aware keys.** Key on Clerk `sub` when authenticated, IP+JA4-ish fallback when not. IP alone is wrong for Sh'elah's actual audience: a yeshiva, day school, or shul behind one CGNAT egress would otherwise share a single bucket and rate-limit each other. Signed-in users get a per-identity allowance; anonymous traffic gets the tighter IP bucket. This also gives §8.B-AGE's age gate something real to hang on.

**L3 — Cost circuit breaker. The layer that guarantees the failure mode.**

`backend/cost_meter.py` today only *records* (`estimate_cost_usd:40`, `record_llm_call:58`). It enforces nothing.

> **⚠️ Updated 2026-08-19 — this paragraph is now partly stale, and the correction matters.** A *per-user* daily ceiling has since shipped (`cost_meter.py:285-312` `check_user_budget_and_enforce`, called at `asgi.py:668`), so `cost_meter.py` does enforce something. But it is **not** the global breaker L3 describes, and it is not sound: it is a check-then-act race, and — more fundamentally — **the `cost_usd` column both mechanisms read is currently \$0.00 for the majority of production calls** because the price table lacks the production Gemini model. **§20 owns fixing that ledger and the per-user race; §16.3-L3 continues to own the global blocking breaker. §20 is a hard prerequisite: a breaker over a zeroed ledger never trips.** Do not implement a price table or a per-user check here — see §20.0's boundary table. Add a **global daily USD ceiling, checked before the provider call**, reusing the existing circuit-breaker primitive from `backend/utils/search_provider.py` (§3 reuse rule — do not invent a second breaker).
>
> **⚠️ Corrected 2026-08-23 (Prompt 45/§33.1) — the primitive named above is wrong; the shipped code deviates deliberately and the deviation is blessed.** `search_provider.py`'s `APIHealth` breaker is an **in-process, per-instance** primitive — exactly the shape §16.1's own D3 defect describes. Reusing it for a *global* ceiling would mean each of Vercel Fluid's concurrent instances tracks its own independent trip state, silently multiplying the effective ceiling by however many instances are warm — the opposite of what "global" means here. `backend/cost_meter.py::is_global_cost_breaker_tripped()` (§16.10) instead reuses **`backend/rate_limit.py`'s Redis-backed shared store** (the same cross-instance-safe store Phase 9a's D3 fix built), via new `get`/`setex` methods and a `get_shared_store()` accessor. This satisfies this section's *intent* (one shared primitive, not a second bespoke store) while deviating from its literal text. **Read this bullet as authoritative going forward: the correct primitive to reuse for any future global/cross-instance state is `backend/rate_limit.py`'s shared store, not `search_provider.py`'s breaker.** Do not "fix" the code back to match the sentence above.

When the breaker trips:

- `/ask` serves cached answers where available, and otherwise a calm, honest state: *"AI answers are paused for today — the full Torah library below is unaffected."*
- **The entire library, siddur, calendar, zmanim and search stay fully functional.**

This is the direct answer to the availability requirement. Degradation is *ordered*: the expensive optional feature sheds first, the site never does. The worst realistic outcome becomes "the chatbot was quiet for an evening," never "the card was drained" and never "shelah.org is down."

### 16.4 Supporting hardening (same phase, small)

- `MAX_CONTENT_LENGTH` on the Flask app **and** an explicit byte cap in the ASGI middleware (the Flask setting does not protect `asgi.py:187`, which is the whole D1 lesson generalized). Suggested 64 KB, plus a hard character cap on `question` *before* tokenization.
- `/api/client-errors`: require same-origin, cap `stack` well below 8 KB, put it in the `telemetry` class, and drop the spoofable `_extract_client_ip()` value from the Sentry context once D2 is fixed.
- Structured `429` with `Retry-After`, a stable machine-readable `code`, and a friendly frontend state (skeleton + countdown, not a wall). WCAG-compliant, `prefers-reduced-motion` honoured, per the project UI rules.
- **Turnstile, not BotID, for anonymous `/ask` beyond N requests/hour.** Vercel BotID's server-side verification SDK is JavaScript-only; this is a Python function, so BotID cannot be wired into `/ask` regardless of plan tier. Cloudflare Turnstile is free, backend-agnostic (one `siteverify` POST from `asyncio.to_thread`/async httpx), and invisible in the common case. Note this explicitly so nobody plans around a tool that cannot be integrated here.
- Observability: emit one structured log line per mitigation with `{tier: waf|middleware|breaker, class, key_hash, route, request_id}`. Hash the key — do not log raw IPs into Sentry (§8.D privacy).

### 16.5 Explicitly rejected

| Rejected | Why |
|---|---|
| Geo / country blocking | Sh'elah's audience is global Jewish communities. Wrong on the merits, and trivially bypassed. |
| `User-Agent`-based blocking | Forged in one line, and UA is not even an available rate-limit key on Hobby. |
| Raising `memory` or `maxDuration` "for resilience" | Directly increases the cost of an attack. Opposite of the goal, and contradicts §14. |
| Caching `/ask` responses at the CDN | Personalized and non-deterministic. Stays `no-store` permanently — §14 already ruled this. |
| In-process locks / counters as a store substitute | Same defect as D3, restated. |
| A second limiter on the FastAPI side | See §16.3 rejection block. |
| CAPTCHA on every request | Punishes the 99.9 %. Turnstile is threshold-triggered for anonymous traffic only. |

### 16.6 Phasing & exit criteria

**Phase 9a — stop the bleeding.** Characterization test proving D1 → single ASGI middleware with the policy table → Upstash store → D2 header fix in both functions → remove Flask-Limiter from `app.py` → body caps. New dependency: Upstash (and `redis`/`limits` as needed). *Exit:* the D1 test now returns 429; `pytest -q` green; no `backend/*` module imports `app`; `graphify update .` shows no new cycle.

**Phase 9b — edge + budget.** WAF rules (Log mode → observe → enforce) + cost breaker in `cost_meter.py` + graceful `/ask`-paused UX. *Exit:* breaker demonstrably trips in a test with a stubbed ledger and the library still serves. **⚠️ Blocked on §20a (2026-08-19):** the breaker reads `ai_usage_log.cost_usd`, which is wrong for most rows today — a stubbed-ledger test would pass while the production breaker never fired, the exact false-confidence outcome this project keeps hitting. Land §20a first. **§20c also depends on this phase:** the recommended answer to the multi-account budget bypass is "the global breaker is the real ceiling," which is only true once 9b ships. **✅ Resolved 2026-08-23 (Prompt 45/§33.5):** Phase 9b shipped 2026-08-22 (§16.10 below); §20c's decision has been made — Option A, see §20.2 PHASE 20c.

**Phase 9c — identity & polish.** Clerk-`sub` keys, Turnstile threshold, structured mitigation logging, `docs/SECURITY.md` + the `RATE_LIMIT_*` env matrix, and correcting `docs/API.md` (which currently documents a 429 that does not exist).

### 16.7 Ordering against the rest of this document

**Phase 9a runs next — before §9 / Prompt 20 (agentic tools), unconditionally.** *(Amended 2026-08-19: §20a — the cost-ledger fix — now runs before Phase 9b, and Phase 9a's own remaining scope has shrunk; see §16.8.2. The rule that nothing agentic ships onto an unmetered `/ask` is unchanged and now has a second reason: §20.1-C1 means the meter reads \$0.00.)* §9 multiplies per-question cost by adding multi-step tool loops on top of each `/ask`. Shipping an agentic loop onto an endpoint with *no* enforced limit, *no* spend ceiling and a *spoofable* key would take a live, unbounded budget hole and widen it. Phase 9b should land before public launch (§8.H gate); 9c can trail.

### 16.8 Reconciliation — what has actually shipped since 2026-07-31 (verified 2026-08-19)

§16.1's four-defect analysis is now sixteen months of commits old in places. Re-read against the working tree today. **Three of the four defects changed state, and one was closed by the route §16.3 explicitly rejected.** Verify these yourself before acting — this table will go stale the same way.

| Defect | 2026-07-31 state | **Verified 2026-08-19** | Evidence |
|---|---|---|---|
| **D1** — limiter bolted to the wrong stack | `/ask` completely unlimited in production | **⚠️ Closed, but by the rejected route.** `asgi.py` now has its own in-process limiter (`_RATE_LIMIT_WINDOW_SECONDS = 60`, `_RATE_LIMIT_MAX_REQUESTS = 20`, `asgi.py:62-63`, checked at `:86`) matching Flask's `RATE_LIMIT_ASK`. Both entry points are limited today — **as two independent mechanisms, not the one unified middleware §16.3-L2 specifies.** | `asgi.py:62-63,86-106`; `app.py:809,1943` |
| **D2** — attacker-controlled rate-limit key | `CF-Connecting-IP` read first, trivially spoofable | **✅ Fully fixed, matches spec.** One canonical `_resolve_client_ip()` in `backend/helpers.py`, shared by both transports (`asgi.py:35,73-76`). Zero `CF-Connecting-IP` reads anywhere in application code. Regression-tested with an explicit "must never regress" comment (`tests/test_helpers.py:37-64`). | `backend/helpers.py`, `asgi.py:73`, `tests/test_helpers.py:37` |
| **D3** — per-process counters | `memory://`, no shared store | **❌ Open, and now doubly so.** `RATELIMIT_STORAGE_URI` still defaults to `memory://` (`app.py:820`) and `asgi.py`'s limiter is a plain in-process `OrderedDict` of deques with no store abstraction at all. The code carries its own honest marker: `app.py:810` reads *"KNOWN GAP (plan.md §16.1 D3, not fixed by this change)."* | `app.py:810,820`; `asgi.py:52-107` |
| **D4** — every route but `/ask` unlimited | `RATE_LIMIT_DEFAULT` parsed to `[]` | **✅ Fixed.** `RATE_LIMIT_DEFAULT = ["60 per minute"]` (`app.py:805`), applied at `:835-836`. Every Flask route without a specific `@maybe_limit` now inherits a default. | `app.py:805,835-836` |
| **Body caps** (§16.4) | absent | **✅ Shipped.** `MAX_CONTENT_LENGTH` on Flask plus an independent `Content-Length` check in `asgi.py`'s middleware — the D1 lesson generalized correctly. `/ask`'s question was already capped by `sanitize_user_query`'s `MAX_INPUT_CHARS`. | — |

### 16.8.1 The finding is now sharper than "no shared store"

**The real defect today is two independently-maintained limiter implementations, not merely an unshared counter.** `DECISIONS.md` names this precisely, and it matters because it changes what "done" means:

- Two policy definitions as **independent literals** — `app.py:809` `RATE_LIMIT_ASK = "20 per minute"` and `asgi.py:62-63` `_RATE_LIMIT_WINDOW_SECONDS = 60` / `_RATE_LIMIT_MAX_REQUESTS = 20`. They agree today. **Nothing enforces that they keep agreeing** — no shared constant, no test asserting equality, no comment on either linking to the other.
- Two key functions, two 429 body shapes, two config surfaces, two failure postures.
- **`/api/stack/health` reports the Flask-side number** — i.e. it advertises a limiter that is not the one enforcing production traffic. An operator reading that endpoint gets a confidently wrong answer.
- This is **exactly the outcome §16.3's rejection block was written to prevent**: *"two limiters, two stores, two key functions, two 429 body shapes and two config surfaces which will drift apart… precisely the 'live, divergent duplication' failure mode §2 identifies as this project's most dangerous anti-pattern, re-created deliberately in the security layer."* It was re-created anyway — pragmatically, to close D1 quickly, which was the right call under time pressure and is now a debt with a name.

The lineage is worth stating plainly, because it recurs: §2 (backend `helpers.py`), §19 (frontend inline vs modules), §22 (`/ask` pipelines), and now §16 (rate limiters). **Four instances of one failure mode.** Whatever closes this should be shaped so the fifth is harder, not just so the fourth is patched.

### 16.8.2 What Phase 9a now means

§16.6's Phase 9a was written when D1/D2/D4 were open. Its remaining scope is smaller and more specific:

1. **The characterization test §16.1 demanded is now moot for D1** — `/ask` does return 429 today. Replace it with the test that actually matters: **assert the two limiters' effective policy is identical**, and make it fail if either literal is edited alone. Cheapest sound form: one shared constant module both import, plus a test asserting `asgi`'s window/max and Flask's `RATE_LIMIT_ASK` describe the same policy. That is a genuine step toward L2 even if the middleware unification never lands.
2. **D3 is the substantive remaining work** — the Upstash-backed shared store, and with it §16.3-L2's single ASGI middleware replacing both limiters. Unchanged from the original spec; §16.3 remains correct on the design, including the deliberately asymmetric fail-open/fail-closed posture.
3. **Delete the D2 and D4 steps from Phase 9a's checklist.** They are done; leaving them listed invites a future pass to "fix" `_resolve_client_ip()` back into something worse.
4. **Sequencing changed:** §20a (cost-ledger integrity) now precedes Phase 9b, and Phase 9a's shared-policy test is small enough to land alongside it.

**Net position for §16 as a whole: the limiter is no longer decoration — it is a real per-instance speed bump on both transports with a non-forgeable key. It is still not a global ceiling, and it is now implemented twice.** That is the accurate sentence; §16.0's "appearance of rate limiting and effectively none of the substance" is no longer true and should not be quoted as current.

### 16.9 Reconciliation — Phase 9a shipped (2026-08-22)

§16.8.2 scoped the remaining Phase 9a work down to two items: land a shared-policy equality test, then build D3's Upstash store and the §16.3-L2 unified middleware. Both are done, and the unification changed what the equality test needed to be.

- **One middleware, not two limiters.** `backend/rate_limit.py`'s `RateLimitMiddleware` (registered via `fastapi_app.add_middleware(RateLimitMiddleware)` in `asgi.py`, ordered before `request_id_middleware` so a 429 still carries a `request_id`) is now the single enforcement point for every native FastAPI route and every Flask route reached through the `WSGIMiddleware` mount. Flask-Limiter is deleted from `app.py` entirely — the `Limiter` construction block, `maybe_limit()`, `RATE_LIMIT_DEFAULT`/`RATE_LIMIT_ASK`/`RATE_LIMIT_FEEDBACK`, and the `@errorhandler(429)` are all gone. `asgi.py`'s old `_rate_limit_store`/`_check_rate_limit`/`_rate_limit_key`/`_RATE_LIMIT_*` constants are gone too.
- **§16.8.2 point 1's equality test is moot by construction, replaced by something stronger.** There is only one `_POLICIES` dict (`backend/rate_limit.py:67`) now — nothing to compare against a second literal, so a drift-detection test has no second party to check against. What actually proves the unification: `tests/test_ask.py::TestAskRateLimit` and `tests/test_routes_feedback.py::TestFeedbackRateLimit` drive real traffic through `fastapi_client` (the ASGI layer, where production traffic actually goes) up to `_POLICIES["llm"].max_requests` / `_POLICIES["feedback"].max_requests` and assert the next request 429s — reading the limit from the live policy table rather than a hand-copied literal, so the test can't silently drift from what's enforced. `tests/test_rate_limit_config.py` covers the `RATELIMIT_ENABLED` kill switch the same way, against `backend.rate_limit` directly.
- **D3 (`backend/rate_limit.py:172-190`).** `RATE_LIMIT_REDIS_URL` (Upstash, `rediss://`) is read at import time; unset, the middleware falls back to an in-process `_InMemoryStore` and logs a loud startup warning naming this exact gap. **This is still an open per-deployment configuration step, not a code gap** — production is not yet verified to have `RATE_LIMIT_REDIS_URL` set; that's an infrastructure/env-var task, not a follow-up prompt.
- **Fail-open/fail-closed posture, as specified.** `_POLICIES["llm"].fail_open = False`; every other class is `True`. A store outage on `/ask` fails closed (alarmed to Sentry via `_capture_backend_error`); every other class fails open. Matches §16.3-L2 exactly, not simplified.
- **Identity-aware keying, partial.** `backend/auth.py`'s new `extract_user_id_from_bearer_value()` (a framework-agnostic sibling of the Flask-only Clerk verification, shared by both `app.py` and `asgi.py`/`backend/rate_limit.py` — another §2-duplication instance closed, see §16.8.1's lineage list) lets the `llm` class key on Clerk `sub` when a valid bearer token is present, IP otherwise. This is narrower than §16.3-L2's full spec (anonymous vs. authenticated *tiers* with different limits, not just a different key) — full identity-aware quotas remain Phase 9c, deliberately deferred, not a regression.
- **`/api/stack/health` no longer lies.** Its `security` block now reports `backend.rate_limit.RATELIMIT_ENABLED`, `RATE_LIMIT_REDIS_URL`-presence, and the live `_POLICIES` table directly — the exact §16.8.1 finding ("`/api/stack/health` reports the Flask-side number... an operator reading that endpoint gets a confidently wrong answer") is closed.
- **What Phase 9a did *not* touch, on purpose:** the Vercel WAF edge layer (§16.3-L1, still unconfigured), the cost circuit breaker (§16.3-L3 / Phase 9b, blocked on §20a per §16.7), and Turnstile/full anon-vs-auth tiering (§16.4, §16.6 Phase 9c). These are unstarted, not silently dropped.
- **Verified:** `pytest -q` green (full suite, 91% coverage); `python -c "import app, asgi, backend.rate_limit"` clean; `graphify update .` shows no new import cycle and no `backend/*` → `app` top-level edge (`backend/rate_limit.py` imports only `backend.auth`, `backend.helpers`, `backend.logging_setup`, `starlette.*`).
- **Known test-suite fragility, not introduced by this pass but worth naming:** `tests/test_ask.py`'s non-rate-limit `TestAskFastAPI`/etc. tests call `/ask` via `fastapi_client` without an `X-Forwarded-For` override, so they all share one IP-keyed bucket in the in-process store for the lifetime of the pytest process (module-level singleton, not reset between tests). That bucket currently sits at 19 of the `llm` class's 20/min ceiling — the same numeric coincidence existed with the old `asgi.py` in-process limiter pre-unification, so this is not a regression, but it means one more unadorned `/ask` call added anywhere in that file trips a 429 in an unrelated test. Any new test that hits `/ask` via `fastapi_client` without asserting on rate-limit behavior should set a unique `X-Forwarded-For` (see the reserved `192.0.2.x` TEST-NET-3 addresses already in use: `.99`, `.100`, `.150`) or the shared-bucket count should be pushed into a `conftest.py` fixture that resets `backend.rate_limit._store` between tests.

### 16.10 Reconciliation — Phase 9b shipped, L3 + frontend UX (2026-08-22)

§16.6's Phase 9b exit criterion was: *"breaker demonstrably trips in a test with a stubbed ledger and the library still serves."* §20a (the ledger fix this phase was blocked on) shipped 2026-08-19; this pass built the breaker itself plus the graceful `/ask`-paused UX. L1 (WAF) is specified but not yet applied — still an operator/dashboard action, unchanged in kind from §16.3-L1's original framing.

- **L3 — global cost breaker, `backend/cost_meter.py::is_global_cost_breaker_tripped()`.** Reads the same `_daily_budget_usd()`/`_fetch_today_usage_rows()` primitives `check_daily_budget_and_alert()` already used (not a second daily-total computation — §2's reuse rule), caches the computed total for 30s to bound Supabase read volume under sustained `/ask` traffic, and fails open on any cache or Supabase-read error (alarms via `_capture_backend_error`, consistent with every other Supabase-touching function in this module). **One deliberate deviation from this section's literal text, flagged for explicit sign-off — see §33.1:** §16.3-L3 says to reuse "the existing circuit-breaker primitive from `backend/utils/search_provider.py`." This pass reused `backend/rate_limit.py`'s Redis-backed shared store instead, because `search_provider.py`'s `APIHealth` breaker is exactly the **per-instance, not cross-instance-safe** primitive §16.1's own D3 defect describes — reusing it here would silently recreate D3 for the one mechanism meant to be the authoritative cross-instance ceiling. `backend/rate_limit.py` gained `get`/`setex` on its store abstraction plus a `get_shared_store()` accessor for exactly this reuse, rather than opening a second Redis connection.
- **Wired into the real `/ask` path**, not just built and tested in isolation: `asgi.py::ask_async()` checks the breaker after the strict-mode block and before AI synthesis; when tripped, it serves a cached answer if one exists for the same `(question, language, mode, lens, user)` key (the previously-dead `ASK_RESPONSE_CACHE`/`_get_cached_ask_payload`/`_set_cached_ask_payload` mechanism in `app.py` is now actually written-to and read-from in production for the first time), otherwise the calm paused-state payload §16.3 specifies verbatim (`meta.breaker_tripped: true`, `meta.fallback: true`, `sources`/`wiki`/`customs` still populated from the already-computed context — the library stays functional, exactly the ordered-degradation goal).
- **Frontend UX (Part C) shipped.** `templates/index.html`'s `renderDisclaimerBanner()` gained a `breakerTripped` parameter, checked ahead of the existing `safety_class` referral branch (the two are mutually exclusive in practice — the breaker payload always reports `safety_class: "ok"` — but checked first regardless). Reuses `#aiDisclaimerBanner`/`#readerDisclaimerBanner` and the existing `--ai-warning-*` tokens via a new `.ai-disclaimer-banner--breaker-paused` CSS class (same color pair as the safety-referral variant, `font-weight: 400` not `600` so a routine budget pause doesn't visually compete with an actual safety referral), icon+text pairing (no color-only state), `role="status"`. Verified live in-browser both themes (light: `#fef6e7`/`#c8902a`/`#7a4a06`; dark: `#1a1208`/`#3a2c10`/`#c09840` — both are the pre-existing, already-contrast-verified referral-banner token pair). **No `Retry-After` countdown was built — see §33.6:** the original Part C text modeled a countdown on `formatCountdownDuration`/`startCountdown`, which key off a `Retry-After` *header* from a `429`; the breaker path is a `200` with no such header, so there is no real data source for a countdown today. Shipped a static "try again after midnight UTC" string instead, matching the backend copy.
- **Not done — real, not silently dropped:** the WAF L1 layer (spec written, `docs/SECURITY.md` §7, not yet entered in the Vercel dashboard — operator action); `DAILY_BUDGET_USD` is still empty in `.env.example`/production per §24.4, so the breaker is live code with an inert threshold until that's configured; §14.7.1/§14.7.4's baseline and preview-deploy verification (§33.4); §20c's multi-account-bypass decision, now newly unblocked by this landing (§33.5).
- **Verified:** `pytest -q` green (91.09% coverage); `python3 -c "import app, asgi, backend.cost_meter, backend.rate_limit"` clean, including after fixing a real circular import found during this pass (§33.7); `graphify update .` run.

---


## 17. Sentry — vendor onboarding snippets and the required deviations from them

Recorded 2026-07-31, from Sentry's own onboarding wizard for the two projects created for Sh'elah. **These snippets are reference material, not the specification.** Four of Sentry's defaults are wrong for this application and one of them is a security defect. The deviations in §17.3 are binding; where they conflict with the vendor snippet, the deviation wins. Implementation is Prompt 6b.

### 17.1 Projects & credentials

| Project | Sentry platform | Env var | Secrecy |
|---|---|---|---|
| Backend | **Python → FastAPI** (`FastAPI` is *under* `Python` in the picker — one selection, not two) | `SENTRY_DSN` | **Server secret.** Never commit. |
| Frontend | **Browser JavaScript** (not React — no React surfaces exist) | `SENTRY_DSN_BROWSER` | Public by design; still read from env, never hardcoded. |

Org `o4511830797975553`; backend project `4511830920462337`, browser project `4511830963257344`. **The 32-hex public key in each DSN is deliberately redacted from this document.**

> ⚠️ **Why the backend DSN is not written down here.** A DSN cannot *read* data from Sentry, so it is often treated as harmless. That reasoning does not hold at Sh'elah's tier: the free Developer plan is **5,000 events/month**, and anyone holding the backend DSN can POST forged events straight to Sentry's ingest and exhaust that quota — **blinding error monitoring for the rest of the month without ever touching shelah.org.** That is the §16.2 `/api/client-errors` attack with the application removed from the loop. Both DSNs live in Vercel env vars only. §10 plans a public GitHub presence; a hardcoded DSN in `plan.md` would ship with it.

### 17.2 Vendor snippets (as issued by Sentry, unmodified — for reference only)

**Browser — install & init**

```bash
npm install --save @sentry/browser
```

```javascript
import * as Sentry from "@sentry/browser";

Sentry.init({
  dsn: "https://<REDACTED>@o4511830797975553.ingest.us.sentry.io/4511830963257344",
  dataCollection: {
    // To disable sending user data and HTTP bodies, uncomment the lines below:
    // https://docs.sentry.io/platforms/javascript/configuration/options/#dataCollection
    // userInfo: false,
    // httpBodies: []
  }
});
```

Verification snippet: `myUndefinedFunction();`

Replay suppression (Sentry's documented method):

```javascript
Sentry.init({
  replaysSessionSampleRate: 0,
  replaysOnErrorSampleRate: 0,
  // don't include `new Sentry.Replay()` in integrations
});
```

**Python / FastAPI — install & init**

```bash
pip install "sentry-sdk" "fastapi"
```

```python
from fastapi import FastAPI
import sentry_sdk

sentry_sdk.init(
    dsn="https://<REDACTED>@o4511830797975553.ingest.us.sentry.io/4511830920462337",
    # Add data like request headers and IP for users
    send_default_pii=True,
)

app = FastAPI()
```

Verification route Sentry suggests:

```python
@app.get("/sentry-debug")
async def trigger_error():
    division_by_zero = 1 / 0
```

Sentry's note: the FastAPI integration auto-enables whenever `fastapi` is installed; init must run *before* the app is constructed; lower `traces_sample_rate` to reduce performance volume.

### 17.3 Required deviations — binding

| # | Vendor default | Verdict | Why |
|---|---|---|---|
| **1** | `send_default_pii=True` | ❌ **Must be `False`** | Attaches request headers, cookies and IP. On Sh'elah that sweeps in Clerk session tokens and can carry the user's question. Halachic questions are routinely medical, marital, mental-health and abuse-adjacent — the exact material §8.B's classifier exists to route. Violates §8.D outright. **This is the single most dangerous line in the vendor snippet.** |
| **2** | Hardcoded `dsn="https://…"` | ❌ **Env var only** | See §17.1. Both DSNs come from `os.environ` / a template-rendered value, and both paths are true no-ops when unset — mirroring the existing discipline at `backend/logging_setup.py:33–47`. |
| **3** | `@app.get("/sentry-debug")` doing `1 / 0` | ❌ **Never ship to production** | An unauthenticated route that reliably throws. Until §16 Phase 9a lands it is also unrate-limited, so ~5,000 calls drain the monthly quota and blind monitoring — the §17.1 attack, self-hosted. Permitted only behind an explicit non-prod guard, and deleted once verification passes. Sentry can also be verified from a preview deploy without adding a permanent route. |
| **4** | `npm install --save @sentry/browser` | ⚠️ **Does not apply yet** | The frontend has **no build step** until Prompt 11 (§7g) replaces the Tailwind CDN. Load the browser SDK from Sentry's CDN with an **SRI integrity hash** (§7g requires SRI on all CDN assets), degrading gracefully on CDN failure exactly as `static/js/motion.js` already does. Revisit the npm path only after §7g gives the frontend a bundler. |
| **5** | `dataCollection` block left commented out | ❌ **Uncomment and set** | `userInfo: false` and `httpBodies: []` must be *active*, not aspirational. This is the vendor-supported mechanism for the "question text must never reach Sentry" requirement in Prompt 6b STEP 2. `beforeSend`/`beforeBreadcrumb` scrubbing is defence-in-depth on top, not a substitute. |
| **6** | Replay zeroed via sample rates | ⚠️ **Go further — omit the integration entirely** | Setting `replaysSessionSampleRate: 0` stops capture but still ships the Replay code. Do not include the Replay integration at all: smaller bundle, and no config drift can silently re-enable session recording of someone typing a private halachic question. |
| **7** | `traces_sample_rate` as a flat number | ⚠️ **Replace with a `traces_sampler`** | Current value is a flat `0.1` (`backend/logging_setup.py:41`), which spends budget on `/api/health` and static routes. §14 established that Provisioned Memory bills full wall-clock *including* I/O waits, so a span showing a slow Sefaria call inside `/ask` is a **cost** finding, not a perf curiosity. Sample `/ask` and the fan-out routes meaningfully; sample health and statics at **0**. |

### 17.4 Init ordering — already correct, do not regress

Sentry requires `sentry_sdk.init()` to run before the app object is constructed. Sh'elah already satisfies this **by accident of import order, not by design**: `backend/logging_setup.py` runs `init()` at module import (`:37–47`), and `asgi.py:36` imports from it — 138 lines before `fastapi_app = FastAPI(...)` at `asgi.py:174`.

This is load-bearing and undocumented. Prompt 6b must add a comment at both sites recording the dependency, so a future import reshuffle (§4 Phase 4 style) cannot silently move `init()` after app construction and disable instrumentation with no visible symptom.

### 17.5 Product selection — what to turn on

| Sentry product | Decision | Rationale |
|---|---|---|
| **Errors** | ✅ On | Baseline. |
| **Tracing** | ✅ On, via `traces_sampler` | See deviation 7 — latency *is* cost under the §14 meter model. |
| **Profiling** | ❌ Off | Adds overhead to **Active CPU**, the one meter Sh'elah barely touches, and profiles of I/O-bound code mostly show socket waits. Makes §14's numbers worse to learn nothing. |
| **Sentry Logs** | ❌ Off | `_JSONFormatter` (`logging_setup.py:50`) already emits structured JSON to stdout, which Vercel captures. Shipping the same records to Sentry is duplicate infrastructure, duplicate quota, and extra egress from a function billed on wall-clock — the §2 divergent-duplication anti-pattern in observability clothing. |
| **Session Replay** | ❌ Off | Deviation 6. Privacy (§8.D) and quota. |
| **User Feedback widget** | ❌ Off | §12 already plans a feedback mechanism. Build it first-party so responses land in Supabase as *your* data, not behind a third-party free-tier quota. A Sentry widget now means removing it later. |
| **Uptime Monitoring** | ✅ On, **5-minute interval** | Resolves the §14.6.2 ↔ §8.E.2 conflict. Sentry's checks run from Sentry's infrastructure but still invoke *your* endpoint, so the invocation cost is unchanged — a free monitor must not tempt the interval back to 30s (~86k wasted invocations/month). Primary check against a static asset; deep check on `/api/health` at 5 minutes. |
| **Cron Monitoring** | ❌ Off | §14.6 established this repo has no cron jobs and must not acquire any. |

Integrations are **not** hand-picked in `init()` — `sentry-sdk` auto-enables FastAPI, Flask, WSGI, `httpx` and logging. The `httpx` one matters most: every Sefaria and LLM call flows through it, which is precisely where the wall-clock (and therefore the bill) accumulates.

### 17.6 Dashboard settings — manual, and required before launch

Not code, but load-bearing given a 5,000 events/month ceiling. Recorded here so it survives the session; Prompt 6b writes the durable copy into `docs/OBSERVABILITY.md`.

1. **Per-key rate limits on both projects** (Settings → Client Keys → Rate Limits). 5K/month ≈ 170/day. One looping component eats the month in an hour. Tighter on the browser key.
2. **Spike Protection** enabled — backstop for whatever the rate limits miss.
3. **Inbound filters** (browser project especially): browser-extension errors, localhost, known crawlers, legacy browsers. These drop server-side *before* counting against quota — client-side filtering alone still costs the event.
4. **Data scrubbing**: Data Scrubber + default scrubbers + **Scrub IP Addresses**. The IP setting counts twice: §8.D privacy, and until §16 D2 lands the IP the backend attaches is forgeable anyway, so retaining it is worse than useless.
5. **One alert rule** (volume spike → email). Per-issue alerts at this stage teach you to ignore Sentry.
6. **Source maps: deferred** to Prompt 11. No build step exists yet; vanilla JS traces are readable unminified. Documented as deferred rather than pretended-configured.

---

## 18. Verification pass (2026-07-31) — Prompts 1–11 audited against the running codebase, dead-code registry

Ran `claude_code_prompts.md` Prompts 1–11 back against current source (grep/read for backend items, five parallel read-only investigation agents for the frontend items 5/8/9/10/11, each required to cite `file:line` for every claim, cross-checked by hand for the highest-impact ones before being trusted). Full per-prompt evidence lives in `claude_code_prompts.md`'s status table and per-prompt notes; this section is the plan-level summary plus the dead-code/dead-docs registry the audit surfaced. One live bug (§18.2) was fixed in place since it was a one-line, zero-risk correction to already-attempted work, not new scope; everything else is recorded for a future prompt, per instruction — nothing else was deleted or rewritten in this pass.

### 18.1 Prompts 1–11 — verified status

| Prompt | Verdict | Basis |
|---|---|---|
| 1–4b (backend refactor) | ✅ Done | `text_engine.py`/`search_provider.py` contain every symbol the phase specs list, zero duplicate copies remain in `app.py`/`helpers.py`, all `search_provider.py` network calls carry `timeout=` + narrow `except` + circuit-breaker wiring (`sefaria`/`web`/`translate_google`/`translate_mymemory`), `app.py` has zero leftover migrated blocks, loop-bridge + `TTLCache` locking match §4 Phase 5 exactly. |
| 5 (source-box fix) | ✅ Done (2026-08-01) | Both render sites use one delegated `click` listener per container (`event.target.closest(...)`) instead of a per-anchor loop; dead `.ai-source-card` CSS deleted; stagger-delay consolidated to one canonical rule in `loading.css`; `role="status"`/`aria-busy` added to the skeleton region and `#aiSources`, cleared on every fetch exit path. |
| 6 (observability) | ✅ Done | Confirmed via this session's own STEP 0 reconciliation before Prompt 6b work began. |
| 6b (browser Sentry) | ✅ Done | Completed this session — SRI-pinned CDN load, true no-op when unset, question/answer scrubbing (test-verified), CSP updated, `/api/client-errors` hardened, docs written. |
| 7 (docs pass) | ✅ Done (2026-08-01) | `README.md` now cross-links `API.md`/`OBSERVABILITY.md`/`FRONTEND.md`/`SERVICE_ARCHITECTURE.md`/`DATABASE.md`/`DEVELOPER_NOTES.md` in a new "Further Documentation" section. See §18.3 for the doc-archival resolution. |
| 8 (motion overhaul) | ✅ Done (2026-08-01) | motion.dev (Motion One) now loads via a CDN `<script>` (`motion@12.43.0/dist/motion.js`) with a computed-and-cross-verified SRI hash, before `static/js/motion.js` — `window.Motion` populated in production, `ShelahMotion.*` no longer permanently falls back to instant state changes. ~48 hardcoded `transition:` durations across `style.css`/`ai.css`/`sidebar.css`/`calendar.css` migrated onto `--motion-dur-*`/`--motion-ease-*` tokens (new `--motion-ease-decel` token added for a curve repeated 5+ times with no existing match); `transition: all` sites replaced with named properties. Found and fixed a genuine WCAG 2.3.3 bug in `sidebar.css` (a `!important`-on-ID+class animation outranking the global reduced-motion fallback on specificity). |
| 9 (dark mode overhaul) | ✅ Done as scoped — AA fixes landed 2026-08-15 | 7 of 8 feature sheets migrated with genuine, verified auto-flip-token collapses (not just value substitution): `ai.css` 31→13 occurrences, `reader.css` 16→14, `sidebar.css` 12→11 (plus 2 real dead-token-reference bugs fixed — `--text-primary`/`--navy`-style references that silently fell back to hardcoded hex because the token was never defined anywhere), `calendar.css` 11→10; `halacha.css`/`prayer.css`/`typography.css` confirmed already fully token-based. **`style.css` (484 of ~582 total occurrences) deliberately left as-is** — a scripted redundancy analysis found only 17 trivially-duplicate declarations out of ~962 selector/property pairs, confirming its dark-mode *values* are not raw-hex debt; the real reason the occurrence count can't drop is that `style.css`'s LIGHT-mode rules mostly use one-off hardcoded hex rather than the token system, so proving most dark overrides redundant would require inventing ~400+ new tokens with no browser available to visually verify them — user chose not to risk that. AA contrast audit at `docs/ACCESSIBILITY_AUDIT.md`; its 2 genuine issues **resolved 2026-08-15**: `--ink-secondary` had real small-text consumers failing 4.5:1 (calendar Hebrew-date label, reader subtitle, segment badges) — darkened `#64748b`→`#5a6b85`, now 5.06:1/4.63:1, verified in-browser both themes; `--accent-gold`'s 1.97:1 fail has zero live text consumers (icons/borders only) — documented via a code comment, no fix needed. The no-FOUC head script bug fixed 2026-07-31 — see §18.2. |
| 10 (loading-states overhaul) | ✅ Done (2026-08-01) | `#prayerBooksNavList`/`#popularTextsGrid`/`#selectionInsightsHint`+`Body` all now carry `role="status"`/`aria-live="polite"`/`aria-busy`, cleared on every JS exit path. Rebuilt `shelah-shimmer` in `tokens.css` as a `translateX`-driven `::after`-overlay animation (transform-only) instead of animating `background-position` — required restructuring `loading.css`'s shared skeleton rule; discovered and cleaned up `ai.css`'s own skeleton background rules, which turned out to be 100% shadowed dead code (same-specificity, `loading.css` loads later and wins). Deleted `--ai-loading-color` (from `ai.css` and both its `tokens.css` copies — confirmed zero consumers) and the orphaned `#aiSynthesis p.animate-pulse` rule. |
| 11 (frontend platform fixes) | ✅ Done as scoped — CSP nonce deliberately deferred (decided 2026-08-15), see `claude_code_prompts.md` Prompt 31 | Item 1 (Tailwind build step) done, unchanged. Item 2 (CSP): `Strict-Transport-Security` added; the stale Tailwind-CDN-JIT comment corrected — that blocker was already resolved. The nonce/`'unsafe-inline'` question (found 2026-08-01): `templates/index.html` has 112 inline `onclick=` handlers and 43 inline `style=` attributes; CSP2+ browsers drop `'unsafe-inline'` entirely once any nonce is present in the same directive, so adding a nonce without first refactoring those 155 sites would break every onclick handler and inline style site-wide. **Resolved as Option 2 (defer)** — confirmed with the user 2026-08-15; the refactor is available as future work if `index.html`'s markup changes for other reasons first. Item 3 (SRI): unchanged, confirmed not fixable — Google Fonts serves different `@font-face` payloads per User-Agent, so no single static hash is valid. Item 4 (fonts): `terms.html`/`privacy.html`/`accessibility.html` now have `preconnect` for both font hosts, matching `index.html`. Item 5 (SEO/RTL): all three legal pages now have OG/Twitter-card/canonical metadata; `toggleLanguage()` now sets `html.lang` alongside `dir` (confirmed fixed — was dir-only). Server-side dynamic lang/dir via Jinja evaluated: feasible (no Clerk/session dependency needed) but is a genuine small feature addition (cookie + 4 routes + template change), not a bug fix — left as a future prompt candidate rather than implemented here. |

### 18.2 Bug fixed during this pass (not new scope — a defect in already-attempted Prompt 9 work)

`templates/index.html`'s no-FOUC head script read `localStorage.getItem('shelah-theme')` — a key **nothing in the codebase ever writes**. The app's real persisted preference is `localStorage['Sh'elahPrefs'].theme` (`APP_PREFS_KEY`, read by `setThemePreference()`/`applyThemePreferenceClass()` later in the same file). Because the dead key always read `null`, the script's `if (t==='light')` branch could never fire, so **every page load forced `data-theme="dark"` regardless of the user's saved light/system preference**, until the deferred `setThemePreference()` call ran later and silently corrected it — meaning every light-mode user saw a flash of wrong-theme content on every load. Fixed to read the real key, parse it, and fall back to `prefers-color-scheme` for `'system'`/unset — matching `applyThemePreferenceClass()`'s own resolution logic. No test depended on the old key (`grep` confirmed zero references in `tests/`/`tests_js/`); full pytest + node suites re-verified green after the change.

### 18.3 Dead / unused / redundant code and docs — registry, resolved 2026-08-01

Found during the 2026-07-31 audit pass; **all items below were resolved in the 2026-08-01 Phase A–G pass** except `ask_pipeline.py`, which was deliberately deferred per the user's explicit instruction (see Prompt 30).

**Backend — orphaned Phase-1/2 compatibility shims in `app.py` — resolved.** Every genuinely-unused re-export from the Phase-1/2 back-compat blocks was deleted after cross-checking each name against every blueprint's `from app import (...)` block *and* every lazy `import app as _app`/`flask_app_module` dynamic-attribute-access site in the repo (not just static grep, since ruff's F401 can't see dynamic access). Two names ruff flagged as unused were correctly **kept** — `WEB_LAST_RESORT_WARNING` and `_extract_query_keywords` are consumed via `_app.WEB_LAST_RESORT_WARNING`/`_app._extract_query_keywords(...)` in `backend/rag.py`'s lazily-imported module reference, invisible to static analysis. `backend.auth.require_clerk_auth` and other genuinely-dead stray imports (`io`, `send_file`, `typing.Any`, `datetime`, `functools.wraps`, `urllib.parse.quote`, `uuid4`, `jwt`, `sys`, `typing.Optional` in `health_check.py`, `_extract_google_translated_text` in `helpers.py` — actually kept, consumed by `tests/test_helpers.py`, marked `# noqa: F401`, `traceback` in `logging_setup.py`, `time` in `search.py`/`sefaria.py`/`zmanim_engine.py`) were deleted. `ruff --select F401 backend/ app.py asgi.py` is now clean.
- `backend/ask_pipeline.py` — **decided, still not executed as of 2026-08-26 — now open across five passes.** `run_ask_pipeline()`/`AskPipelineResult` (the pre-existing, still-fully-dead half of the module) still exist in the committed file and `run_ask_pipeline()` still never calls `classify_safety()`. §22 (2026-08-19) recommended deleting the whole file plus a parity suite; **that recommendation is itself now stale** — Prompt 20 landed 2026-08-21 and made the module's *other* export, `run_agentic_ask()`, a genuine production importer (`app.py`'s and `asgi.py`'s `/ask` synthesis both call it behind `AI_AGENTIC_TOOLS`). §27.3 re-scoped the decision accordingly: delete only `run_ask_pipeline`/`AskPipelineResult` and their two dedicated dead-code test files, keep the module and `run_agentic_ask`, and still build `tests/test_ask_transport_parity.py`. See §22's updated text and `claude_code_prompts.md` Prompt 30/Prompt 35's correction banners for the full current scope. **None of it has been executed** — confirmed 2026-08-26 by direct inspection: `backend/ask_pipeline.py` still defines both `run_ask_pipeline` (:456) and `run_agentic_ask` (:556) at `HEAD`, `tests/test_ask_pipeline.py`/`tests/test_ask_pipeline_smoke.py` still exist, and no `tests/test_ask_transport_parity.py` exists anywhere in the repo. This is the actionable next step, not a closed decision.

**CSS — dead/redundant rules found during the Prompt 5/9/10 audits — resolved.**
- `.ai-source-card` — deleted (confirmed zero references beyond its own definition).
- `.ai-source-box`'s duplicated stagger-delay rule — consolidated to the one canonical definition in `loading.css`.
- `--ai-loading-color` and the orphaned `#aiSynthesis p.animate-pulse` rule — deleted from `ai.css`, plus 2 further orphaned copies of `--ai-loading-color` found and deleted from `tokens.css` itself during the same cleanup (confirmed zero consumers anywhere in CSS/JS/templates before deleting).

**Docs — stale/orphaned files — resolved.** `docs/MANIFEST.md`, `docs/INTEGRATION_GUIDE.md`, `docs/QUICK_START.md`, `docs/SOURCES_REGISTRY.md`, `docs/CONNECTION_QUICK_REFERENCE.md`, `docs/WORD_LIMIT_FEATURE.md` moved to `docs/archive/` via `git mv`, with one-line "# Moved" redirect stubs left at the original paths (matching the existing 8-doc archive convention). `docs/DATABASE.md`/`docs/DEVELOPER_NOTES.md` unchanged (kept, still current, now also cross-linked from `README.md`).

**CSP nonce hardening blocker — resolved 2026-08-15.** `templates/index.html` has 112 inline `onclick=` handlers and 43 inline `style=` attributes. Removing `'unsafe-inline'` from `script-src`/`style-src` (Prompt 11's original ask) requires refactoring all of these first — CSP2+ browsers drop `'unsafe-inline'` entirely once a nonce is present in the same directive, regardless of whether it's still listed. See `claude_code_prompts.md` Prompt 31 for the full writeup — **Option 2 (defer, keep `'unsafe-inline'`) chosen**, confirmed directly with the user.

No orphaned files were found in `static/js/` — all seven files (`ai-service.js`, `main.js`, `motion.js`, `reader-ui.js`, `sentry-init.js`, `state.js`, `zmanim.js`) are reachable, either directly from a template or via the `main.js` → `state.js`/`reader-ui.js`/`ai-service.js`/`zmanim.js` ES-module import chain (initial grep against `templates/*.html` alone under-counted this — cross-checking `static/js/*.js` internal `import` statements and `static/service-worker.js`'s precache list was necessary to avoid a false-positive dead-file report). **Refined 2026-08-17 — this claim measured the wrong thing; see §19.6.** File-reachability is not call-reachability: `ai-service.js::askAi` is imported by `main.js:2` and published on the `window.ShelahModules` bridge at `main.js:19`, yet is **never invoked** by anything in the repo, and `reader-ui.js::saveSemanticBookmark`'s own binding path is disabled at runtime by the guard at `reader-ui.js:175`. The bridge is 7 exports wide and 0 callers deep. The files are live; several of the call paths behind them are not — which a file-level grep structurally cannot see. Future audits of `static/js/` must test whether exported functions are *called*, not merely whether their file is *imported*.

---

## 19. ⚠️ Critical finding — live, divergent duplication in the frontend (the §2 failure mode, frontend instance)

**Found 2026-08-17.** This is structurally the same defect §2 documents for the backend (`app.py` vs `backend/helpers.py`): two live implementations of one feature, both shipping, free to drift. It was never tracked — `grep` for `ShelahModules` / `askAi` / `fetch('/ask'` across `plan.md` and `claude_code_prompts.md` returned **zero hits** before this section was written. §18.3's closing line ("No orphaned files were found in `static/js/`") is what previously stood in for frontend module health, and it measured the wrong thing (see §19.6).

### 19.1 The structural facts (verified 2026-08-17 — re-verify before acting, these drift fast)

| Fact | Value | Evidence |
|---|---|---|
| `templates/index.html` total | **11,661 lines** | `wc -l` reports 11,660 — the file has no trailing newline, so editor/`Read`-tool numbering (used for every citation in this section) is one higher |
| Single classic inline `<script>` block | **lines 1568–11571** (10,004 lines) | open tag `:1568`, close tag `:11571` |
| Top-level `function` declarations inside that block | **314** | `grep -c "^        \(async \)\?function "` over the block |
| ES-module entry points | `motion.js` `:11578`, `main.js` `:11579` | both `type="module"`, so both run *after* the classic block |
| `static/js/` modules | 7 files, all real, none stubs | `ai-service` 74 L, `main` 33 L, `motion` 226 L, `reader-ui` 197 L, `sentry-init` 348 L, `state` 84 L, `zmanim` 139 L |
| `window.ShelahModules` bridge | built at `main.js:18-26`, 7 members | `askAi`, `loadSemanticBookmarks`, `saveSemanticBookmark`, `prewarmDailyStudy`, `getState`, `setState`, `subscribe` |
| **Bridge consumers** | **zero** | `grep -rn ShelahModules` repo-wide returns only `main.js:18` and `docs/FRONTEND.md` (6 hits, all prose). `templates/index.html` never references it. |

The bridge `docs/FRONTEND.md` §"`window.ShelahModules` Bridge" presents as *the* mechanism by which legacy inline code calls into modules is, in production today, **write-only**. Nothing consumes it. That is the root cause: the migration built the on-ramp and never routed traffic onto it, so each feature that got a module home kept its inline implementation running in parallel.

### 19.2 Duplication A — `POST /ask` (two live implementations, and the module is the *weaker* one)

**This is the correction that matters most: the divergence runs opposite to what was assumed.** The inline copy is not a naive duplicate of a richer module — it is the richer implementation. `ai-service.js::askAi()` is the impoverished one.

| Concern | inline `askWithRetry` + `handleAiSearch` | `static/js/ai-service.js::askAi()` |
|---|---|---|
| Location | `index.html:9357–9542` | `ai-service.js:14–74` |
| Per-attempt timeout | **60 s** `AbortController` (`:9357`, `:9379-9380`) | **none** |
| Retry | **3 attempts**, `1200ms × attempt` backoff (`:9358`, `:9367-9377`) | **none** |
| Retryable statuses | **502/503/504** (`:9388`) | none — no status branching at all |
| Retryable errors | `AbortError`, `TypeError` (`:9396`) | none |
| Attempt-count telemetry | `error.attempts` (`:9400`, `:9406`), logged at `:9532` | none |
| Retry UX | live phase-text update, "Still working — retrying…" (`:9369-9375`) | none |
| Auth headers | caller awaits `authHeaders(...)` (`:9492` → decl `:3769`) | `window.authHeaders` via `buildAuthHeaders()` (`:3-12`) — **module already depends on an inline global** |
| State write | `appState.lastAiQuestion` / `.lastAiResponse` (`:9506-9507`) | `setState({ai:{pending,lastError,lastResponse,lastAnsweredAt}})` (`:25-31`, `:55-62`) |
| Return contract | raw `Response`; caller calls `.json()` and branches on `.ok` (`:9503-9522`) | parsed payload; **throws** on `!ok` (`:49-53`) |
| Error surface | localized modal text + `logReliability` (`:9520-9532`) | throws an `Error` |
| Request body | `{question, mode, community, language}` from `appState.prefs` / `isHebrewMode()` (`:9494-9499`) | same 4 keys, from `getState().prefs` / `options` (`:41-46`) |

**Consequence for the plan:** "delete the inline copy, call `ShelahModules.askAi`" is a **functional regression**, not a cleanup. It would silently remove the retry-and-timeout behavior that exists specifically to fix a real reported failure ("times out after a couple of tries" — see the rationale comment at `:9360-9364`). Per §2's mandate, the step is **reconcile-then-consolidate**: determine which behavior is correct (it is the inline one), make *that* canonical in the module, then delete both old copies. Not blind relocation.

**Stale cross-reference, fix while here:** the comment at `:9763` (`:9364` at the 2026-08-17 audit — it has since moved; re-locate by content, not line) cites "plan.md §7.13". No such subsection exists — current §7 is a flat bullet list with no numbered children. It belongs to the same dead numbering as the surviving `§7.10` references at `:309`, `:310`, `:338`, `:632`, `:640`, `:666`, `:702`. The behavior is real; the anchor is dangling. Re-point it at §19 during Phase 1.

**Also verify, do not assume:** `docs/FRONTEND.md:61` claims `askAi` "handles 429 (rate limit) and 503 (service unavailable) gracefully." It does not — and `grep -c 429 templates/index.html` is **0**, so neither copy handles 429 anywhere. Separately, `claude_code_prompts.md`'s Prompt 29c row already records that `docs/API.md` documents a 429 that does not exist. Same gap, third document. Do not port a 429 path into the module on the strength of the doc alone; if a 429 handler is wanted, that is net-new behavior and belongs to Prompt 29a/29c, not to this extraction.

### 19.3 Duplication B — geolocation / zmanim (a *documentation* duplication, not a code one)

Distinct category from A. Do not lump them.

- Inline `initZmanim()` (`index.html:10475–10543`) owns the entire flow: `navigator.geolocation.getCurrentPosition` (`:10499`), the `localStorage` location cache, the manual-override guard (`:10501-10505`), `POST /set_location` (`:10517`), and hand-off to `fetchZmanimAPI()` (`:10545`). Supporting inline functions: `setZmanimLocationLabel` (`:1717`), `resolveSavedLocationLabel` (`:1733`), `hasRealZman` (`:10452`), `setZmanRowVisibility` (`:10457`), `applyOptionalZmanRows` (`:10463`), `hasMeaningfulLocationDelta` (`:10469`), `formatCountdownDuration` (`:10673`), `formatZmanClockDisplay` (`:10690`), `startCountdown` (`:10706`).
- `static/js/zmanim.js` (139 L) exports **only** `prewarmDailyStudy` and `installDailyPrewarm` — daily-study ref prefetch + service-worker precache messaging. **It contains no geolocation code whatsoever.**

So there is no divergent-code risk here today. The risk is that `docs/FRONTEND.md:78-88` asserts `zmanim.js` exports `installZmanim()` and owns geolocation, IP fallback, `/api/calendar/today`, and 60-second polling — **none of which exists**. Anyone trusting the doc would write code against an API that isn't there. This is the "don't trust the old comment" class of defect, at document scale.

**Update (2026-08-21):** at the operator's request, `initZmanim()` no longer calls `navigator.geolocation.getCurrentPosition` at all — it now always resolves via the cached `localStorage` pick (if any) or hands off with no coords, letting the server resolve location from the `/set_location`-set session cookie, then IP-geolocation, then a fixed default (`get_engine()` in `app.py`, unchanged). `resolveSavedLocationLabel`, `hasMeaningfulLocationDelta`, and the manual-override guard flag (`zmanimLocationManuallySet`) were deleted as dead code — they existed only to arbitrate races against the now-removed GPS callback. (`resolveSavedLocationLabel` was also silently broken in production regardless: it called `nominatim.openstreetmap.org` directly from the browser, which the CSP `connect-src` never allowlisted — see `backend/helpers.py`'s `SECURITY_RESPONSE_HEADERS`.) `Permissions-Policy`'s `geolocation=(self)` is now `geolocation=()`. **This changes Phase 2's extraction target below** — there is no GPS code left to move into `zmanim.js`, and the exit criterion "`navigator.geolocation` appears once in the repo, in `zmanim.js`" (§19 Phase 2, below, and Prompt 32) is now wrong: the correct target is that `navigator.geolocation` appears **zero** times repo-wide, before and after extraction. Re-verify line numbers before acting on anything above this note — `index.html` has moved again.

### 19.4 Duplication C — semantic bookmarks (not previously flagged; the highest-risk item here)

Two live implementations of the same `POST /api/bookmarks/semantic` feature:

- Inline: `saveSemanticBookmark()` (`index.html:5303–5356`) + `getSemanticBookmarkPayload()` (`:5262`) + `setSemanticBookmarkBusy()` (`:5294`), bound to `#semanticBookmarkBtn` at **`:7196`**.
- Module: `reader-ui.js::saveSemanticBookmark()` (`:130-167`) + `installSemanticBookmarking()` (`:169-197`), and `main.js:13` calls `installSemanticBookmarking()` **unconditionally**.

They differ materially: the inline copy retries once on 401 by re-fetching headers (`:5324-5331`), routes unauthenticated users into `handleSignIn()` (`:5311-5314`), and reports via localized `window.alert`; the module copy throws, does no 401 retry, and reports via `postClientError()` to `/api/client-errors`.

**Why there is no double-POST today, and why that is fragile.** `reader-ui.js:175` guards:

```js
if (typeof window.saveSemanticBookmark === "function") { return; }
```

That guard passes **only** because the inline copy is a top-level `function` declaration inside a *classic* script, which implicitly creates the `window` property. Contrast `const appState` at `:2610`, which needed an explicit `window.appState = appState;` at `:2647` to be visible at all.

**This is a live tripwire aimed directly at this migration.** Wrapping the inline block in an IIFE, or converting it to `type="module"` — the exact mechanical step every extraction phase performs — silently stops creating `window.saveSemanticBookmark`, the guard stops firing, and **both** handlers bind to `#semanticBookmarkBtn`: two POSTs per click, two AI summaries billed, two alert dialogs. It fails silently, in production, with no test covering it (`tests_js/` contains exactly one file, `sentry_init.test.js` — there is **no** test coverage for any other `static/js/` module).

**Second defect, same feature.** `main.js:16` calls `void loadSemanticBookmarks()` unconditionally on every page load — an authenticated `GET /api/bookmarks/semantic` whose result is written to `appState.semanticBookmarks` (`reader-ui.js:118-123`). `grep -c semanticBookmarks templates/index.html` = **0**. Nothing reads that key. Every page load pays for a request whose payload has no consumer.

### 19.5 `docs/FRONTEND.md` is itself divergent — audit it as part of this work

Verified line-by-line 2026-08-17 against the actual modules. The doc was written as an intent spec and has been read since as a description of reality.

| `FRONTEND.md` claim | Reality |
|---|---|
| `:3` `index.html` is "11 200 lines" | 11,661 |
| `:61` `askAi` "reads the Clerk token from `window.Clerk.session`" | False — delegates to `window.authHeaders` (`index.html:3769`), which does the Clerk read |
| `:61` `askAi` "handles 429 … and 503 … gracefully" | False — zero status branching; `429` appears nowhere in `index.html` |
| `:61` `askAi` calls `setState({lastAiQuestion, lastAiResponse})` | False — writes `setState({ai:{…}})`, a different shape |
| `:67` `reader-ui.js` "Exports `installReader()`" | False — exports `installGlobalErrorBoundary`, `loadSemanticBookmarks`, `saveSemanticBookmark`, `installSemanticBookmarking` |
| `:71-74` reader behaviours (bilingual columns, prev/next nav, scroll sync) | Not in `reader-ui.js`; all inline (`renderTextRowsForPayload` `:6432`, `renderCurrentTextByLayout` `:6631`, …) |
| `:80` `zmanim.js` "Exports `installZmanim()`" | False — exports `prewarmDailyStudy`, `installDailyPrewarm` |
| `:84-87` zmanim behaviours (geolocation, IP fallback, `/api/calendar/today`, 60 s polling) | None present in `zmanim.js` |
| `:93-99` `main.js` initialises Clerk, merges Supabase prefs, sets theme, registers the service worker | False — `main.js` is 33 lines and does none of these. They are inline: `initClerkAuth` `:3743`, `hydratePreferences` `:7011`, `setThemePreference` `:2735`, `registerServiceWorker` `:3657` |
| `:107-108` sample code calls `installReader(); installZmanim();` | Neither function exists |
| `:116` legacy inline blocks "use the `window.ShelahModules` bridge" | Zero uses |
| `:33-43` state table lists `lastAiQuestion`/`lastAiResponse` as top-level keys | That is the *inline* convention; `state.js:25-40` declares the *module* convention `ai:{pending,lastResponse,lastError}`. Both write the same object (`window.appState`), under two key schemes. |

Treat every `FRONTEND.md` behavioural claim as unverified until re-checked. The doc is not worthless — its **module map, bridge pattern, and "Adding a New ES Module" recipe (`:239-278`) are the correct target architecture** and this plan follows them. It is the *status* claims that are wrong.

### 19.6 Correction to §18.3's closing claim

§18.3 ends: *"No orphaned files were found in `static/js/` — all seven files … are reachable."* That is **true at file granularity and misleading at function granularity.** The audit's test was "is this file imported by something that runs." It was not "is this exported function ever called." Under the second test:

- `askAi` — imported by `main.js:2`, placed on the bridge at `main.js:19`, **never invoked**.
- `saveSemanticBookmark` — on the bridge at `main.js:21`; the module's own internal call path is disabled by the `:175` guard, so it is unreachable too.
- `prewarmDailyStudy` / `getState` / `setState` / `subscribe` — reachable only via internal module calls, never via the bridge.

The bridge is 7 exports wide and 0 callers deep. Not dead files — **dead call paths behind live files**, which the file-level grep could not see. Future audits of this area must test call-reachability, not import-reachability.

### 19.7 Explicitly rejected — do not propose this

**Rejected: relocate the entire ~10,000-line inline block verbatim into a single new `static/js/app.js`.** Suggested by an external review, rejected 2026-08-16, re-rejected on this evidence.

1. It resolves nothing. All three duplications (A, B, C) survive the move intact — they are relocated, not reconciled. The `/ask` implementation would still exist twice, in two files instead of one file and one block.
2. It creates a sixth competing convention alongside `state.js`'s pub/sub store, the `install*()` recipe (`FRONTEND.md:239-278`), the `ShelahModules` bridge, and the per-feature module map — an `app.js` god-module that is by construction none of them.
3. It fights the project's own documented migration grain. `FRONTEND.md` specifies incremental extraction along feature boundaries; a verbatim bulk move is the opposite operation.
4. It is unverifiable. 314 top-level functions with no test coverage outside `sentry_init.test.js`, moved in one change-set, in an environment with no browser — there is no way to establish the move was behavior-preserving. Compare §18.1 Prompt 11 / Prompt 31, where a *smaller* frontend refactor (155 sites) was deliberately deferred on exactly this "not verifiable headlessly" reasoning. This is 2× larger and the same argument applies with more force.
5. It converts the block's scope. Inline classic-script function declarations become `window` properties; module-scope ones do not. That single semantic change is what fires the §19.4 tripwire and would break every one of the 112 inline `onclick=` handlers catalogued in §18.3 / Prompt 31, all in one unverifiable change.

**Rejected: "just delete the inline `/ask` copy and call the bridge."** Per §19.2, the module is the weaker implementation; this is a regression wearing a cleanup's clothes.

### 19.8 The phased plan

Ordering principle: each phase deletes real duplicate code, is independently revertable, and is small enough to reason about without a browser. Smallest and lowest-risk first. **Phase 0 is not optional** — Phases 1+ each convert scope, which is precisely what fires the §19.4 tripwire.

---

#### PHASE 0 — Disarm the tripwire and establish a test floor (prerequisite for everything below)

**Goal:** make the semantic-bookmark double-bind impossible before any scope conversion happens, and stop paying for a request nobody reads.

1. Replace the implicit-global guard at `reader-ui.js:175` with an **explicit, scope-independent** flag. Have `index.html`'s inline block set something deliberate near its bookmark binding (`:7196`) — e.g. `window.__shelahLegacyBookmarkBound = true` — and have `installSemanticBookmarking()` test *that*, not `typeof window.saveSemanticBookmark`. The guard must not depend on classic-script declaration semantics.
2. Make `main.js:16`'s `void loadSemanticBookmarks()` conditional on the same flag, or delete the call. Nothing reads `appState.semanticBookmarks` (`grep -c semanticBookmarks templates/index.html` = 0) — confirm that is still 0 before deleting rather than gating.
3. Stand up the `tests_js/` floor this whole migration depends on. Today it is one file (`sentry_init.test.js`). Add, at minimum: a jsdom test asserting exactly **one** `click` listener ends up on `#semanticBookmarkBtn` under both scope models (inline-global present and absent), and a `fetch`-mocked test asserting exactly **one** `POST /api/bookmarks/semantic` per click.
4. Exit: `npm test` green; the new double-bind test **fails** if you temporarily revert step 1 (prove the test has teeth — a guard test that passes both ways is worthless).

**Phase 0 is one reviewable change-set. Stop. Await command for Phase 1.**

---

#### PHASE 1 — Reconcile and consolidate `POST /ask` → `ai-service.js` (the first real duplication deleted)

**Goal:** one implementation of `/ask`, in the module, with the inline copy's superior behavior preserved exactly.

1. **Port up, don't swap.** Move the inline retry/timeout logic *into* `ai-service.js::askAi()`: `AI_REQUEST_TIMEOUT_MS = 60000`, `AI_MAX_ATTEMPTS = 3`, the `1200ms × attempt` backoff, the `[502,503,504]` retryable-status list, the `AbortError`/`TypeError` retryable-error test, and the `error.attempts` annotation. Port verbatim; do not "improve" it in the same change.
2. **Abstract the UI coupling out.** The inline version writes retry status into `#aiLoadingPhaseText` (`:9369-9375`). A service module must not own DOM ids. Accept an optional `onRetry(attempt)` callback in `options` and have the call site pass the phase-text updater. Same for the localized strings — `t()` stays at the call site.
3. **Decide the return contract explicitly and write it down.** The inline caller wants the raw `Response` (it branches on `.ok` at `:9505` and reads `data.error` at `:9520`); the module currently throws. Pick one — recommend keeping the module's parsed-payload-plus-throw shape and adapting the call site, since it is the ES-module-native contract and the only one the bridge can express cleanly — and state the choice in the module docstring.
4. **Reconcile the two state conventions.** `askAi` writes `state.ai.*`; the inline caller writes `appState.lastAiQuestion`/`.lastAiResponse`. Both mutate the same object (`window.appState`, set at `index.html:2647`). Keep **both** writes during this phase so nothing downstream breaks, and add a `// TODO(§19)` marking the top-level keys for removal once every reader is migrated. Do not collapse the schemes here — that is a separate change with its own blast radius.
5. **Then, and only then**, delete `askWithRetry` (`:9365-9408`) and rewrite `handleAiSearch`'s try-block (`:9491-9522`) to call `window.ShelahModules.askAi(...)`. This is the **first actual use of the bridge** — expect load-order to matter: modules are deferred, so `ShelahModules` does not exist until after the classic block finishes. `handleAiSearch` only runs on user interaction, so this is safe, but assert it (`if (!window.ShelahModules?.askAi) { /* fallback or explicit error */ }`) rather than assuming.
6. Re-point the dangling `plan.md §7.13` comment (`:9763` as of 2026-08-19, was `:9364` — grep for the text) at **§23.4**, which is now the restored home for that invariant; §19 is the wrong target for it.
7. Correct `docs/FRONTEND.md:61` to describe what `askAi` actually does after this phase — including deleting the false 429 claim.

**Exit criteria:** exactly one `fetch` to `/ask` exists in the repo (`grep -rn "'/ask'\|\"/ask\"" templates/ static/js/` returns one hit, in `ai-service.js`); new `tests_js/ai_service.test.js` covers happy path, 502-then-success retry, 3-attempt exhaustion, timeout/abort, and 4xx-no-retry; `npm test` + `pytest -q` green; `graphify update .`.

**Phase 1 is one reviewable change-set. Stop. Await command for Phase 2.**

---

#### PHASE 2 — Give `zmanim.js` real ownership of geolocation + zmanim rendering

**Goal:** close the doc/code gap in §19.3 by making the doc true, not by editing the doc down.

**Update (2026-08-21):** the extraction list below is stale — `hasMeaningfulLocationDelta` and `resolveSavedLocationLabel` no longer exist (deleted, see §19.3's update note above), and `initZmanim()` no longer touches `navigator.geolocation` or `zmanimLocationManuallySet` (also deleted) at all. Re-derive the current symbol list from the live `initZmanim()`/`fetchZmanimAPI()` before starting this phase rather than trusting the line numbers or symbol names below.

1. Extract, per `FRONTEND.md:239-278`'s recipe, into `zmanim.js` as a new `installZmanim()` export: `initZmanim`, `fetchZmanimAPI`, `hasRealZman`, `setZmanRowVisibility`, `applyOptionalZmanRows`, `formatZmanClockDisplay`, `formatCountdownDuration`, `startCountdown`, `setZmanimLocationLabel`.
2. Build the call-closure first (mirroring §6 step 1) — these reach inline globals (`t()`, `isHebrewMode()`, `zmanimData`, `currentZmanimLocationLabel`, the `ZMANIM_LOCATION_*_KEY` constants). Every one is either a parameter, an import, or an explicit `window.*` read — decide per symbol and list the decisions before moving anything. Do **not** let the module reach for undeclared inline globals the way `buildAuthHeaders` currently reaches for `window.authHeaders`.
3. `main.js` calls `installZmanim()`; the inline `initZmanim()` and its call site are deleted, not left dormant.
4. Update `FRONTEND.md:78-88` to match the shipped exports.

**Exit:** `navigator.geolocation` appears **zero** times repo-wide (it was removed entirely 2026-08-21, not relocated); `tests_js/zmanim.test.js` covers cached-location-first and no-cache/server-fallback; `npm test` green.

**Phase 2 is one reviewable change-set. Stop. Await command for Phase 3.**

---

#### PHASE 3 — Consolidate semantic bookmarks into `reader-ui.js`

Deferred to third because Phase 0 already removed its acute risk, and because it needs a genuine product decision, not just a move: the two copies differ in **user-visible behavior** (inline uses `window.alert` + a 401-retry + sign-in redirect; module throws + `postClientError`). Reconcile deliberately — recommend keeping the inline copy's 401 retry and sign-in routing (real resilience) and the module's `postClientError` telemetry (real observability), dropping `window.alert` for a non-blocking in-page status. Then delete the inline copy, drop the Phase-0 flag and both guards, and let `installSemanticBookmarking()` bind unconditionally.

---

#### PHASE 4+ — Everything with no module home yet (separate phases, one feature boundary each)

The remaining ~290 inline functions are **not** in scope as a unit and must never be moved as one. Candidate boundaries, each its own future phase with its own new module per the `FRONTEND.md` recipe, roughly ascending by risk:

| Candidate module | Representative inline symbols | Rough scale |
|---|---|---|
| `i18n.js` | `t` `:2672`, `isHebrewMode` `:2664`, `translateStaticLabel` `:3050`, `localizeReferenceLabel` `:3110`, `toggleLanguage` `:10137`, ~20 more localize/translate helpers | large but highly self-contained; mostly pure functions |
| `theme.js` | `normalizeThemePreference` `:2678`, `getSystemThemePreference` `:2686`, `applyThemePreferenceClass` `:2701`, `setThemePreference` `:2735`, `cycleThemePreference` `:2745` | small; already token-driven post-Prompt 9 |
| `auth.js` | `isClerkConfigured` `:3690`, `initClerkAuth` `:3743`, `authHeaders` `:3769`, `handleSignIn` `:3788`, `handleSignOut` `:3795` | **do early** — `ai-service.js` and `reader-ui.js` both already reach for `window.authHeaders`; extracting it converts two hidden couplings into real imports |
| `prefs.js` | `savePrefs` `:3904`, `hydratePreferences` `:7011`, `loadPrefsFromServerIfSignedIn` `:3944`, `pushPrefsToServer` `:3995` | medium |
| `library.js` | `buildLibraryIndexMarkup` `:7604`, `loadLibraryIndexViews` `:7679`, `openChapterGrid` `:7858`, `openReferenceGrid` `:8004` | large |
| `prayers.js` | `loadPrayersMenu` `:8198` through `displayPrayer` `:8822`, ~25 functions | large |
| `sidebar.js` / `settings.js` | `applySidebarVisibility` `:4407`, `toggleSettingsMenu` `:4586`, `positionFloatingMenu` `:4499`, mobile drawer set `:4149-4317` | medium; heavy DOM/layout coupling, verify last |
| `reader.js` (extend `reader-ui.js`) | `renderTextRowsForPayload` `:6432`, `renderCurrentTextByLayout` `:6631`, infinite-scroll set `:6136-6432`, selection-insights set `:5522-6078` | largest and most stateful — **do last** |

Sequencing rule: **`auth.js` before any further extraction.** Two modules already depend on `window.authHeaders` existing as an inline global; every additional module that does the same deepens the coupling this whole section exists to remove.

### 19.9 Constraints carried by every phase above

1. **Reconcile-then-consolidate (§2's mandate).** Diff the two copies; decide which behavior is correct and say why; make one canonical; delete **both** originals; point all callers at the canonical one. No phase is complete while two copies of a moved symbol survive.
2. **Move verbatim, then change — never in the same commit.** A behavior fix inside a move is unreviewable.
3. **No module may read an undeclared inline global.** Parameter, import, or explicit documented `window.*` contract. `buildAuthHeaders`'s `window.authHeaders` reach (`ai-service.js:3-12`, `reader-ui.js:6-14`) is the anti-pattern; do not add a third.
4. **Scope-conversion is the hazard.** Every phase that moves code out of the classic block changes whether its declarations land on `window`. Enumerate what the moved code exposed implicitly before moving it.
5. **Test floor first.** No extraction phase lands without a `tests_js/` test that fails when the extraction is reverted.
6. **Headless-verification honesty (the §18.1 Prompt 11 / Prompt 31 precedent).** Anything requiring a real browser to confirm — visual layout, CSP behavior, actual click-through of the 112 inline `onclick=` handlers — is **not** marked done in this environment. Say so explicitly rather than claiming completion.
7. **One phase per change-set. Stop and report between phases.**

### 19.10 Ordering against the rest of this document

Phase 0 is small, self-contained, and closes a live double-POST tripwire — it can run at any time and should not wait on backend work. Phases 1–3 are independent of the §4 backend track (disjoint files) and can run in parallel with it. Phase 4+ should not start until Prompt 31's CSP decision stays settled — the 112 inline `onclick=` handlers catalogued there live in the same block, and a future decision to refactor them would want to be sequenced with, not against, these extractions.

**Tracked as Prompt 32 in `claude_code_prompts.md`.**

---

## 20. ⚠️ Cost-control integrity — the ledger is wrong, and the ceiling that reads it isn't atomic

**Status: ❌ Not started. Written 2026-08-19 from the `DECISIONS.md` audit; every claim below re-verified against the working tree the same day at the cited line.**

**Priority: this is the highest-severity unstarted item in this document, ahead of §16 Phase 9b.** §16's L3 global cost breaker reads `ai_usage_log.cost_usd`. That column is currently wrong for the majority of production rows. Building a breaker on top of a broken ledger produces a breaker that never trips — so §20 is a hard prerequisite for §16.3-L3, not a parallel track.

### 20.0 Scope boundary against §16 (read this before touching either)

Two sections now touch cost. They do not overlap, and must not be allowed to:

| Concern | Owner | Why |
|---|---|---|
| Is `ai_usage_log.cost_usd` *correct*? | **§20** | Measurement layer. Everything else is downstream. |
| Is the *per-caller* daily ceiling enforceable under concurrency? | **§20** | Already shipped (`cost_meter.py:285`); this is a correctness fix to existing code. |
| Is there a *global* daily spend ceiling that blocks? | **§16.3 L3 / §16.6 Phase 9b** | Net-new mechanism, bundled with the WAF/limiter work. |
| Request-rate limiting | **§16** | Orthogonal control. |

If a future pass finds itself specifying a global breaker inside §20, or a price table inside §16, stop — that is the §2 divergent-duplication failure mode reproducing itself in the planning document.

### 20.1 The finding (verified 2026-08-19)

Three defects that compound. Any one degrades cost control; together they mean **there is currently no trustworthy number for what Sh'elah spends, and no enforceable ceiling on it.**

#### C1 — The price table does not contain the production model

| | |
|---|---|
| **Where** | `backend/cost_meter.py:23-35` (`_PRICE_PER_M`), `:41` (`_UNKNOWN_PRICE`), `:46` (lookup) vs `backend/claude.py:62` (`_DEFAULT_GEMINI_MODEL`) |
| **Severity** | Critical — silent |

`_PRICE_PER_M` contains `gemini-1.5-flash`, `gemini-1.5-pro`, `gemini-2.0-flash`. Production runs `_DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"` (`claude.py:62`, overridable by `GEMINI_MODEL`). `estimate_cost_usd()` does `_PRICE_PER_M.get(model.lower(), _UNKNOWN_PRICE)` where `_UNKNOWN_PRICE = {"input": 0.0, "output": 0.0}` — so **every Gemini call, which is every `/ask` call that does not fall through to Claude, is written to `ai_usage_log` at `cost_usd = 0.0`.** No warning, no log line, no metric. The failure is indistinguishable from "we had a quiet day."

Both consumers of that column inherit the error: the per-user ceiling (`check_user_budget_and_enforce`, C2 below) and the global daily alert (`check_daily_budget_and_alert`, `cost_meter.py:171-224`). **Neither can ever fire on Gemini spend.** The only spend that registers today is the Claude fallback path (`claude-haiku-4-5`, priced at `:27`) — i.e. cost is recorded correctly *only* when the primary provider has already failed.

**Two traps the fix must not fall into**, both verified:

1. **The `model=` argument at `claude.py:1435` reads a key named `provider`.** `model=str(primary_result.get("provider") or _DEFAULT_GEMINI_MODEL)`. That key does in fact hold a model name (`"provider": model_name` at `claude.py:580,595,610,1701,1741,1752,1775,1784,1825,1833`; `"provider": _PRIMARY_MODEL` at `:541,550`), so this site is *currently* passing the right string — but the name says otherwise, and a reasonable future edit that makes `provider` hold `"gemini"` would re-break pricing silently. Rename or wrap it as part of this work.
2. **There are four `record_llm_call()` sites, not one** — `claude.py:1433, 1717, 1806, 2000` — and `:2000` is not `/ask` at all (`route="/api/bookmarks/semantic"`). Any fix must cover all four, and any test must assert across all four.

#### C2 — The per-user ceiling is a check-then-act race with no atomicity

| | |
|---|---|
| **Where** | `backend/cost_meter.py:285-312`, called once at `asgi.py:668` |
| **Severity** | High |

```python
total_usd = await asyncio.to_thread(_fetch_today_usage_cost_for_key, key_column, key_value)   # :307
return {"allowed": total_usd < threshold, ...}                                                # :309
```

A read, a comparison, and no write. No transaction, no row lock, no atomic increment, no reservation. Between the read and the eventual `record_llm_call()` write there is a full LLM round trip — hundreds of milliseconds to tens of seconds.

**Concrete exploit:** an authenticated caller at \$1.90 of the \$2.00 default (`PER_USER_DAILY_BUDGET_USD=2.00`, `.env.example:50`) issues 20 concurrent `POST /ask`. All 20 read \$1.90, all 20 pass, all 20 dispatch. Repeatable immediately. Two further properties make it worse:

- **The guard caps the running total, not the single call** — a caller starting the day at \$0 always gets at least one unbounded call through regardless of atomicity.
- **It fails open** (docstring, `:293-294`) — a Supabase blip silently uncaps spend rather than blocking `/ask`. Defensible as a availability choice, but it means the ceiling is only as reliable as Supabase.
- **It is only wired into the async transport.** `check_user_budget_and_enforce` has exactly one non-test call site: `asgi.py:668`. The Flask `/ask` at `app.py:1943` has **no budget check at all**. Today that path is unreachable in production (§16.1 D1 reasoning) — but this is the same latent-divergence shape as §22, and if the Flask route ever becomes reachable the ceiling silently does not exist there.

#### C3 — The global ceiling is an unconfigured, non-blocking alert

`check_daily_budget_and_alert()` (`cost_meter.py:171-224`) sums the day and alerts if over `DAILY_BUDGET_USD`. Its own docstring concedes it is after-the-fact. `.env.example:98` ships `DAILY_BUDGET_USD=` **empty**, so by default it is a no-op; the `budget-check` cron (`vercel.json`, 13:00 UTC, currently uncommitted — see §24.3) fires daily and does nothing observable. Even fully configured it never blocks a request.

The blocking version is §16.3-L3 / §16.6 Phase 9b. §20 does not build it — §20 makes the number it would read true.

#### C3b — A total ledger-write failure is invisible at default log level

`_insert_usage_row()` (`cost_meter.py:50-59`) wraps the Supabase insert in `except Exception` and logs at **`logger.debug`**. Default `LOG_LEVEL=INFO`, so a schema mismatch, a revoked key, or a paused project produces **zero signal** — `ai_usage_log` simply stops receiving rows and every ceiling reads \$0.00 forever. This is the identical failure shape as the `accept_legal()`/`clerk_id` bug in §23.1: broad `except` + sub-INFO logging + a column/table the code was wrong about. Fixing C1 without fixing C3b means the ledger can still go silently dark.

### 20.2 The approach — three stages, in this order

Ordered so that each stage is independently shippable and the *measurement* is trustworthy before anything is built on it.

---

#### PHASE 20a — Make the ledger correct, and make it impossible to silently re-break — ✅ **Done 2026-08-19** (Prompt 33a)

**Goal:** every LLM call writes a non-zero, correct `cost_usd`, and an unpriced model can never again fail quietly.

**Shipped:** `_PRICE_PER_M` (`backend/cost_meter.py`) now has a real entry for `gemini-3.5-flash-lite` — `{"input": 0.30, "output": 2.50}`, verified against `ai.google.dev/gemini-api/docs/pricing` 2026-08-19 (previously missing entirely). `backend/claude.py` exports `get_dispatchable_models()` (a function, not a frozen constant — chosen over 20a.2's literal "module-level constant" phrasing because the STEP-2 teeth test needs to observe a *changed* `_DEFAULT_GEMINI_MODEL` after monkeypatching, which a value computed once at import time cannot do) returning `{_DEFAULT_GEMINI_MODEL, <GEMINI_MODEL env-resolved>, _CLAUDE_FALLBACK_MODEL}`; `tests/test_cost_meter_pricing.py` asserts every name in that set is priced and proves the guard can fail (renames `_DEFAULT_GEMINI_MODEL`, confirms red). `estimate_cost_usd()` now calls `_warn_unpriced_model_once()` on a price-table miss — a WARNING log line plus a Sentry breadcrumb (not `capture_exception` — this is a lighter-weight signal than `_capture_backend_error`), deduped per model name per process via `_WARNED_UNPRICED_MODELS`; still returns `0.0`, never guesses. The `"provider"` key on the six model-calling functions' return dicts (`_call_gemini_model`, `_call_anthropic_httpx_model`, `_call_gemini_httpx_model` — 12 dict-literal sites) is renamed to `"model"` throughout `backend/claude.py`, closing the booby trap where a key literally named `provider` held a model name; `_call_anthropic_httpx_model`'s hardcoded `"claude-haiku-4-5"` literal now comes from a named `_CLAUDE_FALLBACK_MODEL` constant instead of being duplicated as a bare string. `_insert_usage_row()`'s exception handler now calls `_capture_backend_error()` instead of `logger.debug` (C3b) — verified via `tests/test_small_gap_modules.py::test_client_exception_reaches_capture_backend_error`. All four `record_llm_call()` sites (`claude.py:1433,1717,1806,2000`) were already correctly passing `model=` as an explicit keyword distinct from `provider=`; only the one consumer of the internal dict key (`claude.py:1435`) needed the rename. Full pytest green (1391 passed), `graphify update .` run, no new import cycle (the cross-module pricing check lives in the test file, not in a new `cost_meter → claude` runtime import, so the module graph is unchanged).

**Backfill (20a.5) — decision stated, not executed.** `scripts/recompute_ai_usage_log_cost.py` is written and ready (dry-run by default, `--execute` to write, prints pre/post totals per row) but was deliberately **not run** against Supabase in this pass: this repo's `.env` points at what appears to be the only Supabase project this codebase has (no staging environment — see §22's "solo developer, no staging environment" framing), and mutating financial ledger rows in an unattended pass is exactly the kind of action that needs a human reviewing `--dry-run` output first, not a scheduled task executing it autonomously. Until the operator runs it, treat all `ai_usage_log` rows created before 2026-08-19 as **known-wrong for any Gemini-primary call** (cost_usd under-reported, most commonly to exactly $0.00) — this is the documented cutoff timestamp the 20a.5 fallback calls for. Recommend running the script (with `--execute`) and pasting its printed pre/post totals into the commit message once reviewed.

**20a.1 — Golden-master first (project rule, §6.2).** Before changing `_PRICE_PER_M`, add a test asserting the *current* broken behavior: `estimate_cost_usd("gemini-3.5-flash-lite", 1000, 1000) == 0.0`. The fix must flip that test, not arrive alongside it.

**20a.2 — Close the drift at its source, not by adding one row.** Adding `"gemini-3.5-flash-lite"` to the dict fixes today and guarantees the same bug on the next model bump. Take a position:

> **Chosen: a startup/CI assertion that every model the code can actually dispatch is present in `_PRICE_PER_M`, plus a loud runtime fallback.**
>
> - Export the dispatchable-model set from `backend/claude.py` — `_DEFAULT_GEMINI_MODEL`, the `GEMINI_MODEL` env override, and the hardcoded Claude fallback `"claude-haiku-4-5"` (`claude.py:1690`) — as a single module-level constant, so `cost_meter.py` reads model identity from the one place that defines it rather than from a hand-maintained parallel list. This is §2's reconcile-then-consolidate mandate applied to a constant table.
> - Add `tests/test_cost_meter_pricing.py`: for every name in that exported set, assert `_PRICE_PER_M` has an entry and both prices are `> 0`. This is the CI check — it fails on the PR that bumps the model, not three weeks later on the invoice.
> - Change the runtime miss path: `estimate_cost_usd()` on an unpriced model must emit a **WARNING-level** structured log (and a Sentry breadcrumb) exactly once per process per model name, not return `0.0` in silence. Keep returning `0.0` — do not guess a price — but make the gap observable.
>
> **❌ Rejected: deriving prices from a provider pricing API at runtime.** Adds a network dependency on the cost-recording path (which must never block or fail a request), for a table that changes a few times a year. Wrong trade.
>
> **❌ Rejected: just adding the missing row.** Fixes the instance, not the class. The next model rename reproduces it identically, and this document has an explicit rule against that shape of fix (§2).

**20a.3 — Fix the misleading key.** Either rename `primary_result["provider"]` to `["model"]` throughout `backend/claude.py` (13 sites, mechanical), or have `record_llm_call()` take the model from an unambiguous source at each of the four call sites. Prefer the rename: the current name is a booby trap that makes C1 look already-fixed to a reader.

**20a.4 — Make ledger-write failure audible (C3b).** Raise `_insert_usage_row()`'s swallow from `logger.debug` to a `_capture_backend_error()` call (the project's existing structured-error funnel), so a dead ledger reaches Sentry instead of nothing. Keep the write non-fatal to the request — the change is to visibility, not to control flow.

**20a.5 — Backfill decision (must be stated, either way).** Historical `ai_usage_log` rows are wrong and cannot be recomputed exactly (token counts are stored, so a recompute *is* arithmetically possible — decide whether to run it). **Recommend: recompute in place** via a one-off script, since the rows carry `input_tokens`/`output_tokens` and the table is small; record the pre/post totals in the commit message. If instead you accept the loss, add a dated marker row or a documented cutoff timestamp so nobody later reads pre-fix history as real spend.

**Exit criteria:**
- `estimate_cost_usd()` returns non-zero for the actual production model; the 20a.1 golden-master is inverted with a comment explaining why.
- `tests/test_cost_meter_pricing.py` fails if any dispatchable model lacks a price. Verified by temporarily renaming `_DEFAULT_GEMINI_MODEL` and confirming red.
- All four `record_llm_call()` sites covered by test.
- An unpriced model produces a WARNING, not silence.
- A forced `_insert_usage_row()` failure surfaces through `_capture_backend_error`.
- `pytest -q` green; `graphify update .`.

**Phase 20a is one reviewable change-set. Stop. Await command for 20b.**

---

#### PHASE 20b — Make the per-user ceiling atomic — ✅ **Done 2026-08-20** (Prompt 33b)

**Goal:** N concurrent requests from one caller cannot collectively exceed the daily ceiling.

**Shipped:** `scripts/sql/check_and_reserve_user_budget.sql` adds `reserved`/`reservation_id`/`reservation_expires_at` columns to `ai_usage_log` and a `check_and_reserve_user_budget(p_key_column, p_key_value, p_threshold_usd, p_reservation_usd)` `plpgsql` function that, inside one transaction serialized per-key via `pg_advisory_xact_lock(hashtextextended(key, 0))`, sums today's `cost_usd` for the key, denies if `total + reservation > threshold`, else inserts a placeholder reservation row and returns `(allowed, total_usd, reservation_id)`; `REVOKE ALL ... FROM PUBLIC` keeps it service-role-only. `backend/cost_meter.py::check_user_budget_and_enforce()` now calls this via `client.rpc(...)` (`_reserve_budget_or_deny()`) instead of the old read-only `_fetch_today_usage_cost_for_key()` (kept, unused by the checker, for a possible future "usage today" surface); a returned `allowed=False` blocks, transport errors still fail open. The reservation id is threaded through the request via a new `backend/logging_setup.py` contextvar (`bind_budget_reservation`/`get_budget_reservation`, mirroring the existing `bind_user_id` pattern) so `record_llm_call()` **settles** the reservation in place (`_settle_usage_reservation()`, an `UPDATE ... WHERE reservation_id = ...`) instead of inserting a second ledger row; the contextvar is cleared after settling so a second billed call in the same request (e.g. a Gemini-then-Claude-fallback pair) inserts a normal additive row rather than re-settling. Abandoned reservations (process dies between reserve and settle) expire via `expire_stale_budget_reservations()`, folded into the existing `retention_enforce()` cron in `backend/routes_privacy.py` rather than a new job, per the plan's recommendation. STEP 4's per-request ceiling is `_max_single_ask_reservation_usd()` — a fixed ~$0.021 worst-case estimate derived from the existing `max_tokens` dispatch caps (3072 primary + 1024 fallback = 4096 output tokens) and a conservative input-token estimate, priced at the pricier of the two dispatchable non-Gemini-default rates; the existing `max_tokens` values themselves were left unchanged (already an effective cap at the provider). STEP 5: `asgi.py`'s native FastAPI `/ask` route is registered before Flask's WSGI `Mount`, and Starlette matches routes in registration order, so Flask's `/ask` is provably unreachable in production; this is pinned by `tests/test_cost_meter_budget_atomicity.py::test_flask_ask_route_is_unreachable_behind_the_asgi_mount` rather than duplicating the budget check into the dead Flask path (§2). The concurrency test (STEP 0) fires 20 concurrent `check_user_budget_and_enforce()` calls against a fake Postgres client whose `.rpc()` handler is backed by a real `threading.Lock` (run via `asyncio.to_thread`, so genuine OS-thread concurrency, not just coroutine interleaving) for a key $0.003 under a $2.00 threshold with a $0.002 reservation; it asserts exactly 1 of 20 is allowed, and a companion test drives the same scenario through the *old* `_fetch_today_usage_cost_for_key`-based read to confirm it would have let all 20 through, as a permanent regression record of the defect being fixed. Full `pytest -q` green (1408 passed). One bug found and fixed during verification, not scoped to 20b itself: see the new Prompt 38 / §25 below — the shared test fixture's generic Supabase mock returns a `{"data": [], "error": None}` envelope for every POST, which doesn't match a real PostgREST response shape (a bare array for table ops, the function's return value for RPC); `_reserve_budget_or_deny()`'s parsing was tightened to require an actual list with an `"allowed"` key before trusting it, so a malformed/unrecognized response shape fails open instead of `bool(None)` silently reading as a deny.

**Chosen: a Postgres function doing check-and-reserve in one statement, called in place of the current read.**

Rationale: the ledger already lives in Postgres, the check already round-trips to Postgres, and Postgres gives atomicity for free. Anything else adds infrastructure to solve a problem the existing infrastructure solves.

1. New SQL file `scripts/sql/check_and_reserve_user_budget.sql` defining a `plpgsql` function that, in a single transaction, sums today's `cost_usd` for the key, compares against the threshold, and — if under — inserts a **reservation row** (an `ai_usage_log` row with an estimated/placeholder cost and a `reserved` marker column) before returning `allowed`. The reservation is what closes the race: concurrent callers see each other's reservations because the read and the write are the same statement.
2. `check_user_budget_and_enforce()` calls that function via RPC instead of `_fetch_today_usage_cost_for_key`. Keep the fail-open posture on *transport* errors (documented, deliberate), but a returned `allowed: false` must block.
3. `record_llm_call()` **settles** the reservation with real token counts rather than inserting a second row, so the ledger doesn't double-count. This is the fiddly part — spec the reservation lifecycle explicitly (reserve → settle, or reserve → expire) before writing code, including what happens when the process dies between reserve and settle (recommend: a TTL sweep in the existing `retention-enforce` cron, `routes_privacy.py:272`, rather than a new job).
4. **Per-request ceiling, not just cumulative.** Add a hard cap on a single call's estimated cost so a first-call-of-the-day caller can't get one unbounded request through (C2's second property). Cheapest form: cap `max_output_tokens` at dispatch — bounding the call at the provider is stronger than bounding it in the ledger.
5. **Wire the check into both transports or document why not.** If the Flask `/ask` (`app.py:1943`) stays unreachable, add a test asserting it is unreachable rather than leaving the asymmetry undocumented — same discipline §22 applies to the pipelines.
6. **This SQL file must be committed** (§23.2 — five of its siblings currently are not).

**Concurrency test is the deliverable, not a nice-to-have:** fire N concurrent `check_user_budget_and_enforce()` calls against a real or faithfully-faked Postgres with a key already near threshold, and assert at most one passes. A test that only exercises the sequential path proves nothing about the defect being fixed.

**Exit criteria:**
- The concurrency test fails against the pre-fix implementation and passes against the new one — verified both ways (see §25 for how the pre-fix baseline was reconstructed, since it was never committed on its own).
- Ledger totals after a settled reservation match a non-reserved control.
- `pytest -q` green (1408 passed).
- `scripts/sql/check_and_reserve_user_budget.sql` committed.

**Phase 20b is one reviewable change-set. Stop. Await command for 20c.**

---

#### PHASE 20c — The multi-account bypass: decide, don't drift

The ceiling keys on Clerk `user_id`. Clerk signup is frictionless by design. **A caller who hits the ceiling creates a second account and continues.** No accounts-per-person limit, no device/IP linkage for budget purposes, nothing.

This is a product/abuse decision, not a bug with an obvious fix, and it must be **explicitly resolved rather than left as an unspoken known-bad**. Three positions:

| Option | What it costs | What it buys |
|---|---|---|
| **A — Accept and document.** Per-user ceiling is a good-faith guardrail against accidental/single-user runaway, not an anti-abuse control. The real anti-abuse controls are §16's L1 WAF (IP+JA4, survives account rotation) and L3's global breaker (caps total spend regardless of how many accounts). | Nothing to build. | An honest, defensible answer to "what stops someone draining your card" — *"account-level is a guardrail; the global breaker and the edge WAF are the actual ceiling."* Requires §16 Phase 9b to actually land for that sentence to be true. |
| **B — Add a secondary IP/fingerprint budget key** alongside the user key; a caller must be under **both**. | Punishes shared-egress networks — a yeshiva or day school behind one NAT would rate-limit each other, the exact failure §16.3-L2 already calls out for rate-limit keys. Needs the same identity-aware treatment. | Meaningfully raises the cost of the bypass without new infrastructure. |
| **C — Friction at signup** (Turnstile / verified email / age-gate coupling, §8.B-AGE). | Conversion cost on a product whose whole point is accessibility to learners. | Attacks the root — makes accounts non-free. |

> **Recommend A, conditional on §16 Phase 9b shipping.** B's collateral damage lands squarely on Sh'elah's actual audience (institutional shared egress), and C taxes every legitimate learner to stop an attacker who can still buy email addresses. A is only honest if the global breaker exists — so **if §16 Phase 9b is not going to be built, A is not available and B becomes the default.** Record whichever is chosen here with a date; do not leave this table unresolved, because "we have a \$2/day per-user cap" is currently a claim this project cannot fully defend.

> **✅ Decided 2026-08-23 (Prompt 45/§33.5) — Option A chosen.** §16 Phase 9b shipped 2026-08-22 (§16.10): the global breaker (`backend/cost_meter.py::is_global_cost_breaker_tripped()`) and the WAF L1 spec (`docs/SECURITY.md` §7) both exist, satisfying A's stated precondition. Per-user `PER_USER_DAILY_BUDGET_USD` is now documented (`.env.example`, `docs/SECURITY.md`) as a good-faith guardrail against accidental/single-user runaway, not an anti-abuse control — the actual ceiling against a motivated multi-account attacker is the global breaker (caps total spend regardless of account count) plus the edge WAF (IP+JA4, survives account rotation) once L1 is entered in the Vercel dashboard (§33.8, still an open operator action). **One caveat this decision does not resolve:** `DAILY_BUDGET_USD` (the global breaker's own threshold) is still empty in `.env.example`/production as of this date (§24.4, §33's STEP 6) — until an operator sets a real value, the global breaker itself is inert, which means Option A's honesty currently rests on the WAF landing, not the breaker. B and C were not pursued — no new evidence changed their cost/benefit tradeoff from the table above.

**Exit criteria for 20c:** a dated decision written into this subsection, the corresponding `.env.example` / `docs/SECURITY.md` text updated to describe the *actual* guarantee, and — if A — an explicit dependency note added to §16.6 Phase 9b. **All three done 2026-08-23 — see this subsection, `.env.example:50-55`, `docs/SECURITY.md` §5, and §16.6 below.**

### 20.3 Ordering

20a is small, self-contained, and unblocks every downstream cost claim — **run it next, before §16 Phase 9b and before §9/Prompt 20 (agentic tools), which multiplies per-question cost onto a ledger that currently reads zero.** 20b follows. 20c is a decision that can be made in parallel with either but must not be skipped.

---

## 21. ⚠️ Postgres RLS — verify it is real, or stop calling it defense-in-depth

**Status: 🟡 Unblocked 2026-08-24 — §21.2.1 answered (Clerk Third-Party Auth confirmed enabled), §21.2.2's live acceptance test not yet built. Written 2026-08-19. Part operator action, part code — see §21.2.**

This is the one finding in this document that **cannot be resolved by reading or writing code alone**, and that is precisely why it has survived unanswered. It needs a live check against the deployed project plus a test that makes the answer permanent.

### 21.1 The finding (verified 2026-08-19)

RLS on `user_preferences`, `study_bookmarks`, `user_memories` compares `auth.uid()::text = user_id`. `auth.uid()` resolves a Clerk-issued JWT **only if the Supabase project is configured to trust Clerk as a third-party JWT issuer** — a dashboard setting that exists **nowhere in this repository** and is unverifiable from a checkout.

If that setting is absent, `auth.uid()` returns NULL for every request, every RLS policy evaluates false, and every route using the user-scoped client **silently returns zero rows for every real user** — not an error, not a 403, just empty results.

**The blast radius is wider than `DECISIONS.md` recorded.** It lists two consumers of `_get_user_scoped_supabase_client()` (`app.py:1061`). Grep finds **four** production call sites:

| Call site | Route / feature | Silent-failure symptom if `auth.uid()` is NULL |
|---|---|---|
| `backend/routes_user.py:227` | `GET`/`PUT /api/user/preferences` | Preferences appear to save, reload as defaults |
| `backend/routes_user.py:299` | `GET`/`POST /api/bookmarks/semantic` | Bookmarks vanish on reload |
| `backend/rag.py:308` | user-memory read (personalization context) | Personalization silently degrades to generic |
| `backend/rag.py:405` | user-memory write | Memories never persist |

The `rag.py` pair is the dangerous one: unlike preferences or bookmarks, **a user cannot tell that personalization silently stopped working.** There is no visible artifact to notice missing.

Compounding evidence that this is genuinely unresolved rather than merely undocumented — `backend/routes_privacy.py:13-16` states in its own module docstring that gating the DSR endpoints behind the user-scoped client "would 403 for exactly the users this feature exists to serve." That reads as *"we tried it, it did not work for real users, we routed around it"* — which is a data point that `auth.uid()` does **not** currently resolve, not a neutral architectural preference.

Two further inconsistencies, lower severity, resolve as part of the same pass:

- **`ask_history` uses a different RLS idiom** — `current_setting('request.jwt.claims', true)::jsonb ->> 'sub'` instead of `auth.uid()` (`scripts/migrate_ask_history.sql:34-36`). It is never queried through a user-scoped client, so this policy has never once been exercised. Two idioms, one of them dead.
- **`ai_usage_log` has no RLS at all**, and it holds raw `client_key` (IP-derived strings) and per-caller spend. Safe today only because every access path uses the service-role client. Becomes an exposure the moment the anon/publishable key is ever pointed at it.

### 21.2 The approach — an operator action paired with a code-verifiable acceptance test

The point is to make *"is RLS working?"* a question with a **committed, automatically-rechecked answer**, so it stops being something anyone has to guess at. One without the other fails: a dashboard toggle nobody can verify from the repo is how this happened; a test with no dashboard config just documents that it is broken.

**21.2.1 — OPERATOR ACTION (Akiva, not an engineering pass). ✅ ANSWERED 2026-08-24 — confirmed enabled.** Recorded with a date in `docs/SECURITY.md` §2. That string was the single point of failure for four production code paths and lived in no versioned artifact until now.

The second half of the ambiguity — plain default Clerk session token vs. a `"supabase"`-templated one — is resolved by code reading, not a dashboard toggle: `templates/index.html`'s `authHeaders()` sends the plain default token (confirmed 2026-08-24, see Prompt 34's update). `scripts/clerk_supabase_rls.py` (which expects the templated one) is therefore the stale/unused path — it has zero production importers. **Still open:** delete `scripts/clerk_supabase_rls.py` or explicitly document it as an unused reference implementation, per §21.2.1's original instruction to "delete or fix the one that is wrong."

**2026-08-24 update (code-only, does not resolve 21.2.1):** confirmed by reading `templates/index.html:4074-4091` (`authHeaders()`) and `app.py:1017-1046` (`_get_request_supabase_client`/`_get_user_scoped_supabase_client`) that the client→backend→Supabase leg of the JWT actually works as this section already assumed — the untemplated Clerk session token is attached as `Authorization: Bearer` on every authenticated fetch and forwarded verbatim as the Supabase client's Authorization header. This session had no live Supabase SQL/dashboard access (its Supabase MCP connection exposes only read-only log search), so it could not check whether `auth.jwt()` actually resolves claims in Postgres or run the cross-user RLS test — 21.2.1 is still unanswered and `scripts/verify_rls.py` still does not exist.

**21.2.2 — CODE-VERIFIABLE ACCEPTANCE TEST (the durable half).** A test that fails loudly if RLS silently no-ops. Take a position on which kind, because the obvious one does not work:

> **❌ Rejected: a unit test with a mocked Supabase client.** The entire existing suite already does this — every `_get_user_scoped_supabase_client` reference in `tests/test_routes_user.py` (10 sites, `:138`–`:295`) is a monkeypatched fake. A mock cannot tell you whether `auth.uid()` resolves in a real project, which is the only question being asked. Adding more of these actively makes the problem worse by looking like coverage.
>
> **✅ Chosen: a live integration check, run on demand and in CI-with-secrets, that performs a real round trip as a real signed-in user.**
>
> 1. A script (`scripts/verify_rls.py`, committed) that: mints a real Clerk session token for a dedicated test user; calls `PUT /api/user/preferences` with a sentinel value against the deployed URL; calls `GET` back; asserts the sentinel round-trips. Then repeats against `/api/bookmarks/semantic`, and — critically — the `rag.py` user-memory path, since that one has no user-visible symptom.
> 2. **The negative half is what gives it teeth:** with a *second* test user's token, assert that user A's row is **not** returned. A test that only proves "my own data comes back" passes identically whether RLS is enforcing or whether the service-role client is quietly serving everything. Without the cross-user assertion this test is decorative.
> 3. Wire it as a **manually-triggered / scheduled** GitHub Actions job, not a per-PR gate — it needs live credentials and a deployed target, and blocking every PR on a network round trip to Supabase is the wrong trade. Per-PR CI keeps the existing mocked unit tests for orchestration logic; this job answers the different question.
> 4. Extend `/api/devtools/rls-audit` (which already exists) to report the *observed* result of a real user-scoped query rather than only policy presence — so the answer is visible from the running app, not just from a CI log.

**21.2.3 — Fix the two secondary inconsistencies.** Migrate `ask_history`'s policy to the same `auth.uid()` idiom as the other three (or delete it and say in the SQL file that the table is service-role-only by design — either is defensible, drifting between two idioms is not). Add RLS to `ai_usage_log`, or add a comment in its migration file stating explicitly that it is service-role-only and why, so the absence is a recorded decision rather than an oversight.

**21.2.4 — Then, and only then, update the claims.** `plan.md` §8.C.2 and `docs/SECURITY.md` currently describe RLS as an enforced defense-in-depth layer. If 21.2.1 comes back "not configured," those statements are false and must be rewritten to describe what actually protects the data: a hand-written `.eq("user_id", ...)` filter in application code, with **no** database backstop and — today — no test that would catch that filter being dropped from a new route. Add that test too: it is cheap and it is the control that actually exists.

### 21.3 Exit criteria

- A dated line in `docs/SECURITY.md` stating whether Supabase Third-Party Auth for Clerk is enabled, and who verified it.
- `scripts/verify_rls.py` committed, passing, and **demonstrated to fail** when pointed at a user whose data it should not see (prove the negative assertion works before trusting the positive one).
- `ask_history` on one idiom; `ai_usage_log`'s RLS status a recorded decision.
- §8.C.2 and `docs/SECURITY.md` describe the verified reality, not the intended design.
- An application-layer test asserting cross-user isolation on every Supabase-backed route, independent of whether RLS is live.

**§21 is one reviewable change-set once 21.2.1 has been answered. It cannot start before that — the operator action gates it.**

---

## 22. `backend/ask_pipeline.py` — the three-implementation decision, with a recommendation

**Status: ⏸ Decision written 2026-08-19, re-scoped 2026-08-21 (§27.3), still not executed as of 2026-08-26. Supersedes `claude_code_prompts.md` Prompt 30's option list and §18.3's deferral bullet — both of which framed this as open, and it has now been open across five separate passes (2026-08-01, 2026-08-17, 2026-08-19, 2026-08-21, 2026-08-26).**

**⚠️ Re-scope 2026-08-21 (§27.3) — read this before 22.2/22.3 below.** §22.2's original recommendation ("delete `ask_pipeline.py` entirely") was correct when written but is now half-stale: Prompt 20 (§9 agentic tools) landed after this section and made the module's `run_agentic_ask()` export a genuine production importer — `app.py` and `asgi.py` both call it directly behind `AI_AGENTIC_TOOLS`. The module's *other* half — the pre-existing `run_ask_pipeline()`/`AskPipelineResult` this section was actually describing — remains exactly as dead as it was on 2026-08-19. **The current, correct decision is a split, not a whole-file deletion:** delete only `run_ask_pipeline`/`AskPipelineResult` and their two dedicated dead-code test files (`tests/test_ask_pipeline.py`, `tests/test_ask_pipeline_smoke.py`); keep the module and `run_agentic_ask()`; still build the parity suite (22.3.2) regardless. Read 22.2/22.3 below with that substitution — "delete the module" throughout means "delete the dead half of the module," not `git rm backend/ask_pipeline.py`.

### 22.1 What is actually true (verified 2026-08-19)

- `backend/ask_pipeline.py` is **515 lines** and has **zero production importers**. Repo-wide grep for `ask_pipeline` outside the file itself returns only `tests/test_ask_pipeline.py` and `tests/test_ask_pipeline_smoke.py`.
- There are **three** implementations of `/ask` orchestration: `app.py:1490-1975` (sync, Flask), `asgi.py:141-680` (async, FastAPI), and the dead module. Two are live; per §16.1-D1's routing analysis only the ASGI one is reachable in production.
- **Both test files exist only to keep dead code from rotting.** `tests/test_ask_pipeline.py:44` asserts `app.py` still exposes every attribute the dead module expects — its docstring says the point is so the module "doesn't rot silently since nothing else imports it." `tests/test_ask_pipeline_smoke.py` (41 lines) asserts the dead module still imports and still defines `run_ask_pipeline`/`AskPipelineResult`. **Two test files whose entire purpose is maintaining unused code** — a clean signal the current state costs maintenance for no production benefit.
- **The two live pipelines have already drifted, with a shipped bug to show for it.** `.agents/ENGINEERING_RULES.md:54` records it: the ASGI `/ask` handler silently dropped `ai_cited_sources`. `tests/test_ask.py:366` now exists specifically to pin transport parity on the response key set.
- **New divergence found this pass, not in prior audits:** `check_user_budget_and_enforce()` is called from `asgi.py:668` and **nowhere else**. The Flask `/ask` at `app.py:1943` has no per-user spend check at all (§20.1-C2). The two pipelines now differ on a *cost-control* boundary, not just a response-shape one.

### 22.2 The decision

> **Recommend Option B — delete `ask_pipeline.py` and its two test files now, and invest the saved effort in a parity test suite that pins the two live pipelines to each other.**

**Why B over A, stated as a tradeoff rather than a preference:**

| | **A — Finish the migration** | **B — Delete, and pin parity instead** |
|---|---|---|
| Work required | Diff the dead module against *both* current live pipelines (which have moved since it was written), update it to match, cut over two business-critical routes, delete the duplication. Realistically multi-session. | One deletion commit, then a focused parity test suite. |
| Risk profile | Cutting a money-spending, safety-classified, user-facing route over to a 0%-production-exercised implementation. The riskiest change in the codebase. | Deletion is provably zero-risk (nothing imports it). Parity tests are additive. |
| What it actually fixes | The duplication itself — genuinely the better end state. | Not the duplication. Only the *drift*, which is the part that has actually caused bugs. |
| Verifiability here | Needs a live environment to trust the cutover. Per the §18.1-Prompt-11 / §16 precedent, this project does not mark browser- or deploy-dependent work done in a headless pass. | Fully verifiable offline. |
| Failure mode if half-done | A **fourth** state: a partially-cut-over pipeline. This is the third pass to consider finishing it; the base rate is not encouraging. | None — it is one commit. |

The honest framing: **A is the better architecture and B is the better decision given who is executing it and how.** A single developer cutting two live revenue-and-safety-critical routes over to an unexercised implementation, with no staging environment and no way to verify in-browser, is how the *first* drift bug shipped. B accepts the sync/async duplication as a standing, named cost and buys the thing that duplication actually threatens — behavioral agreement — for a fraction of the risk.

**B is only correct if the parity suite is actually built.** Deleting the module and not building the tests is strictly worse than doing nothing: it removes the aspirational fix and adds nothing in its place. Treat 22.3.2 as non-optional, not as follow-up.

### 22.3 Execution (Option B)

**22.3.1 — Delete (re-scoped per the 2026-08-21 banner above — the dead half only, NOT the whole file).**
1. Delete only `run_ask_pipeline()`/`AskPipelineResult` (and their direct-only helpers, if any are unused by `run_agentic_ask()`) from `backend/ask_pipeline.py`, plus `tests/test_ask_pipeline.py` and `tests/test_ask_pipeline_smoke.py` in full — those two files exist solely to keep the dead half from rotting and have no reason to survive once it's gone. Read both test files first to confirm neither also covers something reused elsewhere — `test_ask_pipeline.py` exercises `_flatten_sources_for_ai` (`:52-66`), which must be confirmed to exist only in the dead code path (not also used by `run_agentic_ask()`) before its tests go with it. **Keep the module file itself and `run_agentic_ask()` (plus everything it calls) — do not `git rm backend/ask_pipeline.py`.**
2. Grep repo-wide for `run_ask_pipeline` / `AskPipelineResult` specifically (not `ask_pipeline` — that name now legitimately survives via `run_agentic_ask`) and confirm zero remaining code references outside the two test files being deleted (prose in `plan.md`, `DECISIONS.md`, and `claude_code_prompts.md` does not count).
3. Leave §9 / Prompt 20 (agentic tools) as the place the shared pipeline already lives — say so explicitly in the commit message, since `run_agentic_ask()` staying is the point, not an oversight.
4. Record the LOC/coverage delta.

**22.3.2 — Pin the two live pipelines (the actual deliverable).** A parity suite in `tests/test_ask_transport_parity.py` asserting the Flask and ASGI handlers agree on **the specific things that have already drifted or now differ**, not a vague "outputs match":

| Invariant | Why it is on this list |
|---|---|
| Identical top-level response key set on **every** path — success, strict-block, AI-failure fallback | The `ai_cited_sources` bug (`ENGINEERING_RULES.md:54`); partially covered today by `test_ask.py:366`, extend rather than duplicate |
| Identical retry/timeout behavior | Named in `ENGINEERING_RULES.md:54` as one of the two confirmed production bugs |
| Identical prompt-selection thresholds | Named in `DECISIONS.md` as an already-observed drift |
| `classify_safety()` invoked before every synthesis call, on both | The §8.B / `.agents/ENGINEERING_RULES.md` safety-routing rule; this was the gap that made the dead module dangerous to wire in |
| Per-user budget check present on both, **or** an explicit test asserting the Flask route is unreachable in production | §22.1's new finding — the two now differ on cost control |
| Identical `meta` key set | `ENGINEERING_RULES.md`'s "same JSON key set on every path" rule |

**22.3.3 — Make the duplication a named, documented cost.** Add a short block to `.agents/ENGINEERING_RULES.md`: *"`/ask` is implemented twice (Flask sync, FastAPI async) by deliberate decision (`plan.md` §22). Any change to one MUST be mirrored in the other in the same commit, and the parity suite must be extended if the change adds a new observable behavior."* An accepted cost that is not written down where the next editor will see it is just an unacknowledged bug waiting to recur.

### 22.4 If Option A is chosen instead

Then it must be executed as a real project, not a cleanup: (1) build 22.3.2's parity suite **first**, against the two live pipelines, so there is a specification to migrate toward; (2) rewrite `ask_pipeline.py` against the *current* shape of both — it predates the safety classifier wiring, `check_user_budget_and_enforce`, and the `ai_cited_sources` fix, so this is a rewrite, not a merge; (3) cut over **one** transport (ASGI first — it is async-native and the only one live) and verify in production behavior/logs before touching the other; (4) never cut both in one change-set. Budget it as multi-session work with a live deploy in the loop.

### 22.5 Exit criteria

Either (re-scoped, Option B) `backend/ask_pipeline.py` contains only `run_agentic_ask()` and its helpers — no `run_ask_pipeline`/`AskPipelineResult` — with `tests/test_ask_transport_parity.py` covering all six invariants above, or (Option A) one transport demonstrably delegates to a rewritten shared pipeline with the parity suite green. **Whichever is chosen, update §18.3's `ask_pipeline.py` bullet and `claude_code_prompts.md` Prompt 30's row in the same commit** — this is the fifth document to describe this as undecided or unexecuted, and that pattern is itself the §2 failure mode applied to planning artifacts.

---

## 23. Schema & documentation provenance — stop the drift that already shipped a bug

**Status: ⚠️ Partial, landed 2026-08-21 (Prompt 36).** Written 2026-08-19; §23.2.2 (`ai_usage_log`'s reconstructed `CREATE TABLE`) shipped 2026-08-20 in a separate pass. This pass closed §23.2.1, §23.2.4, and §23.2.5/§23.3; §23.2.3 is built but not fully executable from this environment — see the evidence below and exit criteria (§23.5).

**§23.2.1 (commit the five files):** provenance headers added to all five previously-untracked migration files, each honestly stating *"provenance unknown as of 2026-08-21"* rather than guessing a date — no direct Postgres/`information_schema` access exists in this environment to confirm whether/when they were actually applied to the live project. **Not yet `git commit`ed** — this pass only edited the working tree; committing is a standing-policy action left for the repo owner (or an explicit follow-up instruction), same as every other file this pass touched.

**§23.2.3 (generated `docs/DATABASE.md`):** `scripts/sql/introspect_schema.sql` (new) defines `public.get_schema_snapshot()`, a read-only RPC following the exact pattern `scripts/sql/check_and_reserve_user_budget.sql` already established (a server-side SQL function invoked via `.rpc()`, `REVOKE ALL FROM PUBLIC`) — chosen over a direct-Postgres-connection-string design because no other script in this repo uses one, and introducing a new credential type (`psycopg2` + a `DATABASE_URL`) for one script would be its own inconsistency. `scripts/generate_database_doc.py` (new) calls it and renders `docs/DATABASE.md` deterministically, with a `--check` mode that diffs against the committed file (ignoring the header's run-date/project-ref, so re-runs against an unchanged schema don't show a spurious diff). **Verified reachable**: run live against this environment's real `SUPABASE_URL`/`SUPABASE_SECRET_KEY` and it fails with the correct, actionable error — `get_schema_snapshot() RPC call failed ... has scripts/sql/introspect_schema.sql been applied to this project's SQL editor yet?` — confirming both that the credentials are live and that the RPC function genuinely doesn't exist on the project yet. **Applying that SQL file was deliberately not done here** — it is a live schema change to shared production infrastructure, squarely the kind of action that needs the operator's own hand on it, exactly like every other `scripts/sql/*.sql` file in this repo says ("Run this once in the Supabase SQL Editor"). Until it's applied and the generator run for real, `docs/DATABASE.md` is an **interim hand-reconstruction** (rewritten 2026-08-21 directly from `scripts/sql/*.sql`/`scripts/migrate_*.sql`, cross-checked against every table name/column the backend actually reads or writes), clearly banner-labeled as such with instructions to replace it with the generator's real output. The "share one scheduled workflow with §21.2.2's RLS check" deliverable is **not done** — §21 itself is still `❌ Not started` (see that section's own status), so there is no RLS-check workflow yet to share one with.

**§23.2.4 (close the swallow):** audited every Supabase write path (`.insert(`/`.upsert(` call sites) across `backend/`; `cost_meter.py`, `routes_privacy.py`, and most of `routes_user.py` were already routing failures through `_capture_backend_error()` (confirmed by prior passes). Found and fixed three that weren't: **`routes_user.py::accept_legal()`** — the exact function §23.1's motivating bug is about — was still swallowing via a bare `app.logger.warning()`, the same *shape* of invisible failure as the original `on_conflict="clerk_id"` bug, just spelled differently; and `rag.py`'s **`_store_ask_history()`** (the defensibility-logging table) and **`_store_user_memory_summary()`** both used a bare `except Exception: return` with zero observability. All three now call `_capture_backend_error()` while remaining exactly as non-fatal as before (never blocks the user-facing response) — three new regression tests assert the capture actually fires on a simulated Supabase failure, matching the pattern `tests/test_routes_privacy.py::TestDeleteClerkUser::test_missing_secret_key_reaches_capture_backend_error` already established.

**§23.2.5 / §23.3 (dangling citations):** all eleven external citations to the dead `§7.13`/`§7.14`/`§7.10.2`/`§3` numbering (`backend/claude.py`, `backend/helpers.py` ×2, `app.py`, `tests/test_ask.py` ×3, `tests/test_config_lint.py`, `.agents/ENGINEERING_RULES.md`, `templates/index.html`, `backend/routes_privacy.py`) repointed — the `§7.13`/`§7.14` invariant citations now cite `§23.4` (their restored home), and `helpers.py`'s CSP/`X-XSS-Protection` citation and `routes_privacy.py`'s retention-window citation now point at where that content actually lives today (`§8.C`, `§8.D`) rather than either the dead numbering or a guessed target. **New `tests/test_config_lint.py::TestPlanMdCitationsResolve`** is the "control that makes this the last time": it scans every `.py`/`.sql`/`.html`/`.md` file likely to carry a citation (broader than the dead-config lint's `backend/`-only scope) for `plan.md §N` and asserts each citation's *top-level* section number still has a `## N.` heading in `plan.md` — deliberately top-level-only, not full dotted-path validation, since the large majority of real citations (`§8.B.6`, `§20.1-C2`, `§14.4.4`, etc.) reference a numbered item inside a section's prose, not a separate heading, and validating those would need a full outline parser rather than the "cheap, one regex each side" check this section asked for. This intentionally does **not** flag the six internal `plan.md`-self citations to `§7.10`/`§7.3`/`§7.2`/`§3.15`/`§1.4` the prior pass left unresolved by design (their top-level sections — 7, 3, 1 — still exist; only their now-deleted subsections don't) — no special-case exception list was needed for that, the top-level-only design already doesn't reach them. Verified the lint actually has teeth (not a no-op): a synthetic `§99` citation fails it; every real citation in the repo today passes. Full `pytest -q` green (targeted run: 1595 passed, 0 failed; coverage 91.58%, gate 85%). `graphify update .` run.

This section exists because documentation drift in this repo is **not a tidiness problem — it has already caused a silent production data-loss bug**, and the conditions that produced it are all still in place.

### 23.1 The finding (verified 2026-08-19)

**`docs/DATABASE.md` — the document that is supposed to *be* the schema reference — is wrong in almost every particular.** Verified table-by-table against the SQL files and the querying code:

| `docs/DATABASE.md` says | Reality |
|---|---|
| Tenant key is `clerk_id` (`:3, :17, :25-28, :42, :61, :71, :86, :109, :113`) | `user_id` everywhere. Wrong in ~10 places including the RLS policy examples. |
| `rag_identity_cache` table (`:36-50`) | Does not exist. The linked setup SQL actually creates `community_knowledge` + `user_memories`. |
| `bookmarks` table (`:54-75`) | Real table is `study_bookmarks`. |
| `queries` analytics table (`:79-97`) | Does not exist in any SQL file or any Python file. Pure fiction. |
| Migrations run via `supabase db push` (`:120-127`) | There is no `supabase/` directory anywhere in the repo. Migrations are hand-pasted `.sql` files. |
| Retention table (`:133-140`) keyed on `rag_identity_cache`, `bookmarks`, `queries` | Two of three tables do not exist; the real retention job (`routes_privacy.py:272-307`) enforces 90-day windows on `ask_history` and `ai_usage_log`, neither of which the doc mentions. |

**The bug this already caused:** `backend/routes_user.py:76-82` documents in-code that a previous `accept_legal()` used `on_conflict="clerk_id"` — matching the doc, not the schema. PostgREST rejected every write, a broad `except` swallowed it, and **legal-consent records were never persisted** until someone noticed. That is the exact three-part failure signature this document keeps rediscovering: wrong doc → code written against the doc → broad `except` + sub-INFO logging hides it (identical to §20.1-C3b).

**Compounding: five schema-defining SQL files are untracked in git.** Confirmed via `git status --untracked-files=all`:

```
scripts/migrate_ai_usage_log_add_user_columns.sql
scripts/migrate_ask_history_safety_metadata.sql
scripts/migrate_user_preferences_legal_consent.sql
scripts/migrate_user_preferences_legal_version.sql
scripts/migrate_user_preferences_user_id_to_text.sql
```

These back **currently-used, load-bearing columns** — `safety_class`/`prompt_version` written on every completed answer, the per-user budget columns `check_user_budget_and_enforce()` reads (§20), and the legal-consent versioning (§8.A). Re-clone this repo today and there is no record they were ever written, let alone applied. `ai_usage_log`'s base `CREATE TABLE` is not in the repo **at all**, in any file, tracked or not.

There is also no migration-tracking table, no ordering convention, no checksums, and no rollback SQL anywhere.

### 23.2 The approach

**23.2.1 — Commit the five files. Do this first, today, independent of everything else.** It is one `git add` + commit and it converts five undocumented production schema changes into five reviewable artifacts. Add a header comment to each recording, as far as is known, whether and when it was applied to production — an honest "applied 2026-XX, approximate" beats no record. If provenance is genuinely unknown, write *"provenance unknown as of 2026-08-19"* rather than guessing a date.

**23.2.2 — Reconstruct `ai_usage_log`'s base `CREATE TABLE`.** Derive it from the live schema (see 23.2.3) and commit it as `scripts/sql/ai_usage_log_setup.sql`. A table with no `CREATE` statement anywhere is unreproducible in a new environment, and §20 is about to depend on it heavily.

**23.2.3 — Make `docs/DATABASE.md` generated, not hand-written.** This is the part that stops the drift from recurring; rewriting the prose once fixes today only.

> **Chosen: a committed introspection script that regenerates the doc from the live schema, plus a CI/on-demand diff that fails when they disagree.**
>
> - `scripts/generate_database_doc.py` — connects with the service-role key, introspects `information_schema.columns` / `pg_policies` / `pg_indexes` for the project's tables, and emits `docs/DATABASE.md` in a stable, deterministic format (sorted, no timestamps in the body, so the diff is meaningful).
> - A `--check` mode that regenerates into memory and diffs against the committed file, exiting non-zero on any difference. That is the guard.
> - Run it as the **same manually-triggered / scheduled workflow as §21.2.2's RLS check** — both need live Supabase credentials and a deployed target, both answer "does the repo still describe reality," and giving them one job avoids building two credential paths. **Not a per-PR gate:** blocking every PR on a live database round trip is the wrong trade, and a schema change legitimately lands before the doc regen.
> - Have the generated doc carry a machine-written header — *"Generated by `scripts/generate_database_doc.py` on YYYY-MM-DD against project `<ref>`. Do not hand-edit."* — so the next person who is tempted to patch the prose sees why not.
>
> **❌ Rejected: adopting the Supabase CLI migration workflow** (`supabase/migrations/`, `supabase db push`, shadow-DB diffing) as part of this section. It is the industry-standard answer and genuinely better, but it is a workflow migration with real risk against a live production project holding real user data, and it does not fix the doc — a correct migration history and a wrong `DATABASE.md` can coexist indefinitely. Worth its own future section once the schema is at least *recorded*; sequencing it ahead of 23.2.1 would mean the five untracked files stay untracked while a larger project is scoped.
>
> **❌ Rejected: hand-correcting `docs/DATABASE.md` and moving on.** That is what produced the current state. The doc has been wrong long enough to ship a bug; the failure is the absence of a check, not the presence of typos. Correcting the prose *is* still required — but as output of the generator, not as the deliverable.

**23.2.4 — Close the swallow that hid the bug.** The `clerk_id` failure was invisible because a broad `except` logged below INFO. Audit the Supabase write paths for the same shape — `_insert_usage_row` (`cost_meter.py:50-59`) is a confirmed instance and is already in §20a.4's scope; `routes_privacy.py:301` uses `_capture_backend_error` correctly and is the model to copy. **A PostgREST schema error must never be indistinguishable from success.** Where a write is genuinely best-effort, keep it non-fatal but route it through `_capture_backend_error()`.

**23.2.5 — Fix the dangling `plan.md` cross-references (see §23.3).**

### 23.3 Dangling `plan.md` anchors — ten load-bearing citations pointing at deleted sections

A restructure of §7 removed its numbered subsections. **Ten citations across code, tests, and the engineering rules still point at them**, and this is worse than a cosmetic problem: the sections they cite documented *invariants that tests actively enforce today*, so the specification for live, test-pinned behavior currently has no home in this document.

| Citing file | Cites | What it is actually about |
|---|---|---|
| `backend/claude.py:94` | §7.14 | Shared constant so the two transports can't drift |
| `backend/helpers.py:94` | §7.10.2 | `X-XSS-Protection` removal rationale |
| `backend/helpers.py:949` | §7.14 | `extract_ai_cited` shared so "AI-cited" can't drift |
| `app.py:1758` | §7.13 / §7.14 | AI timeout/retry budget |
| `tests/test_ask.py:302` | §7.1.A / §7.14 | `ai_cited_sources` schema parity |
| `tests/test_ask.py:366` | §7.14 | Flask/ASGI transport key-set parity |
| `tests/test_ask.py:429` | §7.13 | AI timeout/retry resilience |
| `tests/test_config_lint.py:2` | §7.14 invariant #2 | Dead-config lint |
| `.agents/ENGINEERING_RULES.md:54` | §7.1.A, §7.13 | Both confirmed production bugs |
| `templates/index.html:9763` | §7.13 | Client retry budget rationale |
| `backend/routes_privacy.py:60` | §3 retention windows | §3 is "Existing infrastructure this refactor must reuse" — wrong target, not merely stale |

Also internal to this file, all inherited from the same dead numbering and all re-located 2026-08-19 (line numbers shift on every edit — the point is the count, not the coordinates): **`§7.10` ×6** (`:313`, `:314`, `:342`, `:636`, `:670`, `:706`), **`§7.3`** (`:330`), **`§7.2`** (`:339`), **`§3.15`** (`:644`), **`§1.4`** (`:200`), **`§2.2-style`** (`:109`). Seventeen dangling references in total across the repo and this file.

**These internal ones are deliberately left as-is in this pass, and that is a judgement call worth stating:** unlike §7.13/§7.14 — whose content is recoverable from the tests that still enforce them — §7.10, §7.3, §7.2, §3.15 and §1.4 point at a *prior roadmap revision* whose content is not recoverable from anything in the working tree. Rewriting them would mean guessing at what they said. The lint in step 3 below will surface all seventeen; resolving the unrecoverable ones means either finding the old revision in git history or deleting the citation, and both are decisions for whoever runs §23, not something to guess at now.

> **This is not a find-and-replace job.** There is no current section to repoint §7.13/§7.14 *to* — their content was lost in the restructure while the tests that enforce it survived. The fix is to **restore the specification, then update the citations**:
>
> 1. Add a new subsection — **§23.4 below** — as the permanent home for the two invariants, written from the tests that currently enforce them (`test_ask.py:302-430`, `test_config_lint.py`, `ENGINEERING_RULES.md:54`). The tests are the surviving source of truth; read them, do not reconstruct from memory.
> 2. Repoint all eleven citations at §23.4 (or §19/§16 where those are the right target — `helpers.py:94`'s §7.10.2 is a CSP/header item and belongs with §11's frontend-platform lineage, not §23.4).
> 3. Add a lint to `tests/test_config_lint.py` — which already exists for exactly this class of problem — asserting every `plan.md §N` citation in the repo resolves to a heading that exists in `plan.md`. **That is the control that makes this the last time**, and it is cheap: one regex over the repo, one regex over `plan.md`'s headings.

### 23.4 Restored invariant registry (was §7.13 / §7.14)

Reconstructed 2026-08-19 from the tests and rules that enforce these. **Cite this section, not §7.x.**

**Invariant A — AI timeout / retry budget coordination (was §7.13).** Enforced by `tests/test_ask.py:429ff` and referenced by `app.py:1758`, `templates/index.html:9763`, `.agents/ENGINEERING_RULES.md:54`. Origin: a confirmed production bug where the browser aborted `/ask` at 10 s with no retry while the server pipeline plus model retries legitimately needed longer. The rule: **the client's abort budget must exceed the server's worst-case honest completion time, and every layer's timeout must be a deliberate fraction of the layer above it.**

Current layer values, verified 2026-08-19 — *note these are not coordinated and that is a known gap, not a design*:

| Layer | Value | Source |
|---|---|---|
| Vercel function hard limit | `maxDuration: 90` | `vercel.json` |
| Server total AI budget | `AI_TOTAL_BUDGET_SECONDS`, default 45 | env |
| Per-model request timeout | `MODEL_REQUEST_TIMEOUT_SECONDS`, 50 | env |
| Client per-attempt timeout | 60 s | `templates/index.html` (§19.2) |
| Client attempts | 3, `1200ms × attempt` backoff | `templates/index.html` (§19.2) |

**The 50 s per-model timeout can never fire** — the 45 s outer budget always wins first, making `MODEL_REQUEST_TIMEOUT_SECONDS` dead configuration that looks intentional. And **nothing asserts at startup that `AI_TOTAL_BUDGET_SECONDS` stays below `maxDuration`**; it is an env-overridable number guarded only by a comment. Deliverables: (a) make the per-model timeout a deliberate fraction of the total budget rather than larger than it; (b) add a startup assertion that the budget is under the platform limit — `tests/test_config_lint.py` is the natural home; (c) record the intended ordering here so the next tuner has a specification instead of five independent literals.

**Invariant B — transport parity (was §7.14).** Enforced by `tests/test_ask.py:302-366` and `tests/test_config_lint.py`; referenced by `backend/claude.py:94`, `backend/helpers.py:949`. Origin: the ASGI `/ask` handler silently dropped `ai_cited_sources`. The rule: **the Flask and FastAPI `/ask` handlers must return the same JSON key set on every path, and any value both need must come from one shared constant or helper, never two literals.** §22.3.2 extends this into a full parity suite and is the section that now owns it.

### 23.5 Exit criteria

- ⚠️ Five SQL files have provenance headers, ready to commit — **not yet `git commit`ed** (working-tree edit only, per standing git-safety policy). `ai_usage_log`'s `CREATE TABLE` reconstructed and committed (done in an earlier pass, 2026-08-20).
- ⚠️ `scripts/generate_database_doc.py` + `scripts/sql/introspect_schema.sql` written, with a working `--check` mode; verified reachable against the live `SUPABASE_URL` (fails with the correct "RPC not found" error, not a silent crash). **`docs/DATABASE.md` not yet machine-regenerated** — blocked on an operator applying `introspect_schema.sql` in the Supabase SQL editor (a live-infrastructure change deliberately left to the repo owner). Interim hand-reconstruction shipped instead, clearly labeled as such with replacement instructions.
- ❌ The generator + §21.2.2's RLS check share one scheduled workflow — not done; §21 itself is still not started, so there is no RLS-check workflow yet to share one with.
- ✅ Zero `plan.md §N` citations in the repo (outside the six internal, deliberately-unresolved `plan.md`-self citations to dead `§7.10`/`§7.3`/`§7.2`/`§3.15`/`§1.4`) resolve to a non-existent top-level section, enforced by `tests/test_config_lint.py::TestPlanMdCitationsResolve`.
- ✅ §23.4's invariants cited by the code that depends on them (`backend/claude.py`, `backend/helpers.py`, `app.py`, `tests/test_ask.py`, `tests/test_config_lint.py`, `.agents/ENGINEERING_RULES.md`, `templates/index.html`).

---

## 24. Sweep — remaining `DECISIONS.md` findings, triaged

**Status: mixed, see per-item. Written 2026-08-19.** Everything in `DECISIONS.md`'s per-area "Suspicious / thin areas" subsections that is concrete enough to act on and not already owned by §16, §20, §21, §22, or §23. Items judged too vague to turn into a deliverable are listed in §24.7 with the reason, rather than dropped silently.

### 24.1 The Sefaria disk cache is almost certainly a silent no-op in production

| | |
|---|---|
| **Where** | `backend/sefaria_library.py:108` (`_DISK_CACHE_DIR = _PROJECT_ROOT / ".sefaria_cache"`), `:111-140` (`_disk_cache_get` / `_disk_cache_set`) |
| **Severity** | Medium — performance and upstream-blocking risk, not correctness |

The disk tier writes to a path inside the deployed function bundle. Vercel's Python runtime filesystem is **read-only outside `/tmp`**. `_disk_cache_set()` wraps `mkdir` + `write_text` in a bare `except Exception: pass` (`:139-140`) — so in production it fails **silently, every time, with no log line**. The middle tier of a three-tier cache is not merely cold, it is absent, and nothing surfaces that.

Consequence: every cold-start instance has only the empty in-memory `TTLCache` between it and a live Sefaria call. Sefaria has demonstrably 403-blocked this app under load, so this is a real cache-miss-storm risk on any traffic spike or fresh deploy — precisely when it hurts most.

**Approach — decide, then make the decision visible:**

1. **First, verify rather than assume.** Add a one-line INFO log on `_disk_cache_set` failure (bounded — once per process, not per call) and read production logs. `DECISIONS.md` says "almost certainly"; this is cheap to make certain, and the fix differs by answer.
2. If confirmed dead, take a position:
   - **Repoint at `/tmp`** — one-line change, restores a per-instance disk tier that survives within an instance's life but not across instances. Marginal gain over the in-memory tier that already exists, and `/tmp` counts against the function's ephemeral storage.
   - **Delete the disk tier entirely** and be honest that the cache is two-tier in production. Removes ~35 lines and a misleading architecture claim.
   - **Replace with a shared cache** (Upstash Redis / Vercel Runtime Cache) — the only option that actually helps across instances, and it pairs naturally with §16's Upstash dependency.
   > **Recommend: delete the disk tier now, and fold a shared Sefaria cache into §16's Upstash work** as a follow-on once that connection already exists. Repointing at `/tmp` buys almost nothing over the in-memory tier while keeping the "three-tier cache" story that is not true where it matters.
3. Either way: **remove the silent `except`**. A cache tier that cannot report its own failure is how this went unnoticed.
4. Note for local/self-hosted use: the disk cache has **no size bound** (27 MB / 185 files locally already, eviction only by passive TTL-staleness on read). If the tier survives in any form, bound it.

### 24.2 Health endpoints report "healthy" without checking anything

| | |
|---|---|
| **Where** | `asgi.py:277-284`; `backend/routes_devtools.py:57-93` |
| **Severity** | Medium — this is monitoring that will not alarm |

Three defects, all confirmed:

- **`/api/async/health` is a hardcoded stub.** `"flask_mounted": True` (`asgi.py:282`) is a literal constant — it never verifies the mount.
- **`supabase.ready` in `/api/stack/health` is a config-presence check, not connectivity.** Constructing a Supabase client makes no network call, so a wrong key or a paused project reports `ready: true`.
- **`external_apis` lies about Claude/Gemini by omission.** ~~`health_check.py`'s `_circuits` dict tracks Sefaria/Hebcal/Gemini/Claude, but grep confirms `is_healthy` / `record_success` / `record_failure` are called **only** from `backend/utils/search_provider.py`... never from `backend/claude.py`, `app.py`, or `asgi.py`.~~ **✅ Fixed 2026-08-20, Prompt 39/§26.1.** `backend/claude.py` now imports `health` and calls `is_healthy`/`record_success`/`record_failure` at all three `/ask` AI-provider entry points (see §14.4.4's matching correction). `external_apis`'s Gemini/Claude entries now reflect real provider health.

**Deliverables:** make `flask_mounted` an actual probe or delete the field; make `supabase.ready` do a trivial round trip (or rename it `supabase.configured` and say so). ~~Either wire `record_success`/`record_failure` into the four `backend/claude.py` provider call paths... or remove Gemini/Claude from `_circuits` entirely~~ — **done, see above.** `flask_mounted`/`supabase.ready` remain the only two health-field gaps this section names.

### 24.3 Uncommitted `vercel.json`, and the cron rule it contradicts

**This one mostly needs a commit, not a phase.** Confirmed 2026-08-19: `git show HEAD:vercel.json` has keys `["$schema", "functions", "headers"]`; the working tree has `["$schema", "crons", "functions", "headers", "regions"]`. The region pin (`iad1`) and both cron jobs (`/api/devtools/budget-check` 13:00 UTC, `/api/devtools/retention-enforce` 14:00 UTC) exist only in the working tree.

**Action (immediate, no phase needed): commit it.** Until then, three sections of this document rest on config that is not in git — §14.2's region pin, §16.3-L1's "exactly one WAF counter pool" synergy (`:984`), and §20/§8.D's cron-driven jobs.

**Also required — reconcile the contradiction, do not just commit past it.** §14.6.1 states as a hard rule: *"No cron jobs, no keepalive pings, no warmers. Nothing in this repo should exist to keep an instance alive."* Restated in §14.7.5 as a standing `ENGINEERING_RULES.md` entry. There are now two crons.

> They do not violate the *spirit* — the rule targets warmers that convert free idle into billed idle, and these are once-daily admin jobs doing real work (a spend check and a legally-required retention sweep). But the rule as written admits no exception, and nothing documents the reconciliation. **Amend §14.6.1 and the `ENGINEERING_RULES.md` text to read: "No warmers, keepalive pings, or scheduled requests whose purpose is to keep an instance alive. Scheduled jobs that perform real, non-request-path work (spend enforcement, retention) are permitted, must be justified in `plan.md`, and must be counted against the invocation budget in §14.6."** An unamended rule with two live violations trains the next reader to ignore the rule.

**Note while there:** the `budget-check` cron is currently a **no-op in production** — `DAILY_BUDGET_USD` is empty in `.env.example:98` and per `docs/RUNBOOKS.md` unset in production. It fires daily, costs an invocation, and does nothing. Either configure it or disable it; a cron that provably does nothing is billed noise. This is the same operator-action class as §21.2.1 and §24.4.

### 24.4 Environment-configuration defaults that are wrong for production

Three `.env.example` values that a verbatim copy into Vercel's env vars would ship as a live defect. `.env.example` is a template that gets copied — treat its defaults as the values that will actually run.

| Var | Ships as | Risk |
|---|---|---|
| `CLERK_ENFORCE_AUTH` (`:67`) | `false` | Reopens anonymous `/ask`, and the per-user budget key degrades to a shared `ip:` bucket for everyone behind one NAT (§20.1-C2). |
| `DAILY_BUDGET_USD` (`:98`) | empty | The only whole-app spend signal is a no-op (§20.1-C3). |
| `CRON_SECRET` (`:102`) | empty | Gates both cron routes (`routes_devtools.py:263`, `routes_privacy.py:272`). |

**Deliverable:** flip `CLERK_ENFORCE_AUTH` to `true` in the template (fail-safe default; a developer who wants it off can set it off deliberately), add inline `# REQUIRED IN PRODUCTION` markers to the other two, and add a startup check that logs a **WARNING** naming each production-critical var that is unset. Separately — an operator action, same class as §21.2.1 — confirm the live Vercel values for all three and record them in `docs/RUNBOOKS.md`. `CLERK_AUDIENCE` belongs on the same list: unset by default with **no runtime warning anywhere**, not even on `/api/stack/health`, which reports `clerk.configured`/`clerk.enforced` but not whether audience verification is actually active.

### 24.5 Correction to §14.4.4 — a claimed cost win that is not implemented

§14.4.4 lists among latency levers: *"circuit breakers skipping known-dead providers instead of waiting for their timeouts (§4 Phase 3 — already a cost win)."*

**Verified 2026-08-19: false for the AI providers.** §4 Phase 3 wired breakers into `search_provider.py` for Sefaria and web search only. No AI provider call path consults `is_healthy()` before dispatching (§24.2). Every `/ask` attempts Gemini fresh, immediately after consecutive failures, and pays the full timeout each time. Correct the claim in place and move the actual work into §24.2's deliverable, where it doubles as the health-endpoint fix.

**✅ This section's own finding is now stale — fixed 2026-08-20, Prompt 39/§26.1.** The wiring this section called for shipped a day later: `backend/claude.py` consults `is_healthy()` at all three `/ask` provider entry points and reports `record_success`/`record_failure` per attempt. See §14.4.4 and §24.2's matching corrections. Left in place (not deleted) as the historical record of what was found and why — the project's own §23 provenance convention.

### 24.6 Smaller confirmed items — fold into the nearest phase

Each is real, verified, and too small for its own section. Owner noted.

| Finding | Where | Fold into |
|---|---|---|
| Two rate limiters duplicate the same policy as independent literals (`app.py:805,809` vs `asgi.py:62-63`) | app/asgi | **§16.8** |
| `_insert_usage_row` swallows ledger-write failure at `debug` level | `cost_meter.py:50-59` | **§20a.4** |
| `backend/customs.py`'s local-JSON fuzzy-match path is a second, undocumented dead path — same shape as `ask_pipeline.py` but with nothing flagging it | `backend/customs.py` | **§22** — apply the same decide-or-delete discipline in the same pass |
| Cookie-based Supabase-token fallback (`sb-access-token`, chunked cookies) that Clerk never sets — likely dead pre-Clerk leftover | `app.py:1008-1038` | **§21** — confirm dead, then delete; it is an auth-path branch, so it does not get to stay on a guess |
| `tests/test_auth.py` mocks `jwt.decode`/`PyJWKClient` entirely — never exercises a real RS256 verify or JWKS fetch | `tests/test_auth.py` | **§21.2.2** — the live-integration job is the natural home |
| Duplicate byte-identical RLS policy definitions across two SQL files, nothing keeping them in sync | `scripts/sql/SUPABASE_RLS_POLICIES.sql`, `bookmarks_and_preferences_setup.sql` | **§21.2.3** |
| No rollback/`DOWN` SQL anywhere; no migration-tracking table | `scripts/` | **§23.2.1** — note as accepted-for-now with a date, or scope the CLI migration |
| Devtools diagnostic routes (`stack/health`, `reliability`, `rls-audit`) gated only by "is a valid Clerk user" — any signed-up user can read deployment config | `routes_devtools.py` | **§8.C** — needs an admin/role distinction, which the auth model has none of |
| No global exception handler on either framework; calendar/community/library/prayers blueprints log locally and never reach Sentry | blueprints | **§8.E.1** |
| Fully silent exception swallow in the calendar blueprint's parasha lookup — no log at any level | calendar blueprint | **§8.E.1**, same pass |
| Bearer-token verification failures logged at `debug` — a wave of forged tokens produces zero signal at default `LOG_LEVEL=INFO` | auth path | **§8.E.1**, same pass |
| `npm run build:css` wired into neither CI nor deploy; `tailwind.css` is a hand-built committed artifact that can silently drift from templates | `.github/workflows/ci.yml` | **§19** (frontend track) |
| `ruff` and `pip-audit` run `continue-on-error: true` — lint failures and known CVEs never block | `.github/workflows/ci.yml` | **§8.F.1** — decide deliberately; advisory is defensible, undocumented-advisory is not |
| Production never receives the hash-locked, `pip-audit`-scanned dependency set — the lock files are CI-only | `requirements*.txt` | **§8.C.4** |

### 24.7 Deliberately not planned, with reasons

Listed so the omissions are a decision rather than an oversight.

| Finding | Why not |
|---|---|
| "No embeddings / vector search; retrieval is keyword matching with sophisticated-sounding function names" | Not a defect — an explicitly reasoned design choice appropriate to a bounded corpus, and `DECISIONS.md` says so. The only actionable part is **not describing it externally as "semantic search" or "AI-powered retrieval,"** which is a copy/positioning constraint, not engineering work. Belongs in §12.1's About-page copy review, not a phase. |
| Two independent retrieval paths (AI `/ask` vs library reader) not sharing ranking/keyword logic | Real duplication, but `DECISIONS.md` also establishes the two have genuinely different UX requirements, and there is **no evidence of an actual drift-caused bug** — unlike §22, where a shipped bug forced the issue. Unifying is a large refactor justified by a problem that has not occurred. Revisit if a keyword-handling bug is ever fixed in one and not the other; that event is the trigger. |
| `get_halakhic_sources`'s final tier answering from model internal knowledge with only a disclaimer | Named as the pipeline's weakest link, and it is — but the fix is a **product decision about what `/ask` should do when it has no source** (refuse? degrade visibly? answer with a stronger warning?), not a defect with a correct implementation. Needs a product position first; §12.4's answer-feedback loop would supply the data. Deliberately not pre-empted here. |
| Customs JSON→Supabase migration has no visible sync/invalidation mechanism | Too vague to scope from the audit alone — it is unclear whether the JSON is still authoritative for anything after the migration. Resolving that is a prerequisite to writing a deliverable; folded into §22's `backend/customs.py` decide-or-delete item, which will answer it. |
| No session/token revocation check in JWT verification | A standard, well-understood JWT tradeoff, not a defect. The actionable part is **documenting it as accepted** rather than building revocation checking, which needs infrastructure disproportionate to this project. One line in `docs/SECURITY.md`; not a phase. |
| No foreign keys / referential integrity anywhere | Expected and correct given Clerk (not Postgres) owns identity. The residual risk — orphaned rows for deleted Clerk users — is already handled by `delete_account()`; the real question is whether it succeeds on every table, which §21's cross-user isolation test will partly cover. |
| `static/style.css` (4,153 lines) as an unlabeled fourth CSS layer; `index.html` has zero Jinja componentization | Both real, both frontend-architecture debt, both already inside §19's territory. Adding them here would duplicate a section that already owns the file. |
| The 112+ inline `onclick=` / 40+ inline `style=` CSP blocker | Already fully specced and **deliberately deferred with the user's confirmation** — `claude_code_prompts.md` Prompt 31, §18.3, §18.1-Prompt-11. Re-planning it would be exactly the duplicated-drifting-spec failure this document keeps naming. Counts have grown (115 / 40) since the last audit; that belongs as a note on Prompt 31's row, not a new phase. |

### 24.8 Ordering

§24.3's commit is a two-minute action and should happen immediately — several other sections rest on it. §24.4's `.env.example` defaults are similarly cheap and prevent a future foot-gun. §24.1 and §24.2 are small, independent, and can slot into any pass. §24.5 is a one-line correction to be made whenever §14 is next touched. §24.6's items are explicitly homed and should ride along with their host phase rather than being batched.

---

## 25. Test-fixture Supabase mock does not match a real PostgREST response shape (found 2026-08-20, during Prompt 33b)

**Severity:** Medium — not a production defect, but it silently miscategorized a real one. This is exactly the class of gap §6.2's "golden master" discipline exists to catch, and it slipped past because the fixture itself was the thing lying, not the code under test.

**Where:** `tests/conftest.py`'s autouse `mock_outbound_httpx` fixture (`:231-241`). Every outbound POST to `https://mock.supabase.co/.*` — regardless of whether it is a `.table(...).insert()`, `.table(...).update()`, or (new as of Prompt 33b) `.rpc(...)` call — is mocked to return one fixed body: `httpx.Response(200, json={"data": [], "error": None})`.

**The bug this caused:** a real PostgREST endpoint never returns that `{"data":..., "error":...}` envelope shape — a table operation returns a bare JSON array of rows, and an RPC call returns the function's return value directly (for a `TABLE(...)`-returning function like `check_and_reserve_user_budget`, a bare array of row objects). `backend/cost_meter.py::_reserve_budget_or_deny()`'s first cut treated any non-list, truthy `result.data` as a single result row: `rows = result.data if isinstance(result.data, list) else ([result.data] if result.data else [])`. Fed the mocked `{"data": [], "error": None}` dict, that produced `rows = [{"data": [], "error": None}]`, and `row.get("allowed")` read `None` — `bool(None)` is `False` — so **every `/ask` call in the FastAPI test suite was silently denied**, 13 tests in `tests/test_ask.py` failing with `402` instead of `200`. Older call sites (`_fetch_today_usage_cost_for_key`, `_insert_usage_row`) never hit this because they already guarded with a plain `isinstance(result.data, list)` check and fell back to an empty/no-op result — they happened to fail open against this same wrong mock; `_reserve_budget_or_deny`'s "else" branch did not.

**Fix applied in the same pass (not deferred):** tightened `_reserve_budget_or_deny()`'s parsing to require `isinstance(result.data, list)` AND `"allowed" in rows[0]` before trusting the row; anything else — including this mock's shape — now falls into the existing fail-open branch, matching how every other Supabase call site already treats an unrecognized response. This restores behavioral parity with the pre-33b code (`/ask` tests exercise "the check doesn't block," not "the check's deny path," which is already covered directly by `tests/test_cost_meter_budget_atomicity.py`'s dedicated fake-RPC-client tests) without touching the shared fixture.

**What is NOT fixed, and is the actual finding:** the fixture itself is still wrong. `{"data": [], "error": None}` matches no real Supabase REST or RPC response ever observed from PostgREST — it looks like a hand-guess at a generic "empty success" shape, not something copied from a real response body. Every test that relies on this fixture's POST mock (any `.table().insert()`, `.table().update()`, or `.rpc()` call across the whole suite) is currently only protected from a shape-mismatch bug by the same accident of "non-list `result.data` happens to fall through to empty/fail-open" that masked this one — the next hand-written `.rpc()` consumer that parses its result differently (e.g. expects a scalar, or reads `result.data.get(...)` directly) will not be caught by any test, because the fixture cannot produce the response shape that would expose the bug.

**Deliverable:** replace the single blanket POST mock with response-shape-accurate, endpoint-specific mocks — a bare `[]` (or a realistic single-row array) for `.../rest/v1/<table>` table operations, and a bare array of row objects for `.../rest/v1/rpc/<function>` calls, matching each RPC function's actual `RETURNS TABLE(...)` shape (`check_and_reserve_user_budget` returning `[{"allowed": true, "total_usd": ..., "reservation_id": ...}]` is the first real case). Add a regression test asserting the mock's response, when round-tripped through the real `postgrest-py`/`supabase-py` client the way `_get_supabase_client()` constructs it, produces `isinstance(result.data, list) == True` — so a future fixture edit that reintroduces a wrapper-envelope shape fails loudly in the fixture's own test rather than silently miscategorizing whatever code happens to call it next.

**Exit criteria:** `tests/conftest.py`'s Supabase POST mock returns shape-accurate bodies per endpoint (table vs. RPC); a fixture-level test pins the shape so this cannot silently regress; `pytest -q` green; no change to any application code's fail-open/fail-closed posture (this is a test-infrastructure fix, not a behavior change).

---

## 26. Gaps found while implementing Prompts 16–19 (§8.D–H: privacy, reliability, quality, launch gate) (found 2026-08-20)

Prompts 16–19 (`claude_code_prompts.md`) closed out §8.D through §8.H — privacy operations, reliability/observability, quality/accessibility/content-integrity, and business/launch-gate documentation. Most of what those sections called for was already shipped by earlier passes; this round of work found and closed the real remaining gaps (Clerk-delete error visibility, the `hebcal` circuit breaker, the coverage-gate ratchet, an automated a11y CI gate, `docs/CONTENT_QA.md`, `docs/LAUNCH_CHECKLIST.md`). Three genuine gaps surfaced during that work that were deliberately left open — each was out of scope for the prompt that found it, not overlooked. This section catalogs them as their own follow-up so they don't quietly vanish into a "done" status table row. A fourth item (stale lint hygiene) is bundled here because it's cheap and mechanical, not because it's related in kind.

**Section status: ✅ all three items done 2026-08-20 (Prompt 39).** §26.1, §26.2 and §26.3 each carry their own resolution block below with the evidence. §26.2 was the only one that turned out to be hiding a live defect rather than a theoretical hole — its first-ever dark-theme run failed 16 elements on `/` immediately.

### 26.1 `claude`/`gemini` circuit breakers are registered but never consulted before the primary `/ask` call — ✅ **Done 2026-08-20** (Prompt 39)

**Severity:** Medium. Not a correctness bug — the primary AI call still fails safely via its existing `try`/`except` → fallback path. But it means the one circuit-breaker gap left after §26's own hebcal fix is on the highest-traffic, highest-cost path in the app: every `/ask` request.

**Where:** `backend/health_check.py`'s `_PROBES` dict already registers `'gemini'` and `'claude'` (actively probed services, same as `'sefaria'` and `'hebcal'`). But a repo-wide grep for `is_healthy('gemini')` / `is_healthy('claude')` turns up zero call sites in `backend/claude.py`, `app.py`, or `asgi.py`. The fallback stage of `/ask` — `get_halakhic_sources()`, the local-corpus/no-AI-synthesis path — *is* gated on `is_healthy('sefaria')`/`is_healthy('web')` (confirmed by `tests/test_ask.py::TestAskDegradationPath`, added in this same work). The primary stage — the actual Gemini/Claude call that synthesizes an answer — has no equivalent check; it can only degrade by raising and being caught, never by consulting circuit state ahead of the call.

**What this means in practice:** if Gemini (or Claude, as its fallback) is in a known-bad state — three-plus consecutive failures already recorded via `record_failure('gemini')` from a *different* code path, e.g. a background health probe — the primary `/ask` call still dials out and eats the full request latency (and, if it eventually times out rather than erroring fast, potentially the full function-timeout budget) before falling back, instead of skipping straight to the fallback the way `sefaria`/`web`/`hebcal` now do. This is a latency and cost-efficiency gap, not a reliability outage risk — the fallback still fires.

**Deliverable:** before the primary Gemini call (and before the Claude fallback-of-the-fallback, if that exists as a separate call site) in `backend/claude.py`, check `health.is_healthy('gemini')` (`'claude'` for the Claude leg); on an open circuit, skip straight to the next stage in the existing fallback chain rather than making the call, and call `record_failure`/`record_success` around each attempt exactly as the four `hebcal` sites do. Preserve the existing fallback ordering and response shape — this is a "skip the doomed call" optimization, not a behavior change to what `/ask` ultimately returns.

**Exit criteria:** `is_healthy('gemini')`/`is_healthy('claude')` gate their respective calls in `backend/claude.py`; `record_success`/`record_failure` wired symmetrically; a new test forces both circuits open and asserts `/ask` reaches the local-fallback path without attempting either AI call (mirroring `TestAskDegradationPath`'s existing pattern); no change to `/ask`'s response shape or existing fallback-behavior tests.

**Resolved 2026-08-20 (Prompt 39).** The gate went in at the provider layer, not the route layer — `backend/claude.py` imports the `health` singleton (`:27`) and all three `/ask` provider entry points now consult it, which covers both transports without touching a single route handler or response builder. **Sync primary** `_call_gemini_model()`: `if not health.is_healthy('gemini')` at `:563`, ahead of `_configure_gemini_client()`, returning the same `{answer, confidence, error, is_fallback, model}` dict the function's other early-outs already return, with `error="gemini_circuit_open"` — which is not `security_blocked*`, so the existing route logic treats it exactly like any other primary-call failure and falls through unchanged. **Async fallback** `_call_anthropic_httpx_model()`: `if not await asyncio.to_thread(health.is_healthy, 'claude')` at `:1754`, deliberately placed *ahead of* `_get_async_client()`. **Async primary** `_call_gemini_httpx_model()`: same pattern at `:1828`. Both async sites go through `asyncio.to_thread` because `is_healthy()` is not passive — when a down circuit's `RECOVERY_INTERVAL` has elapsed it performs an inline blocking `requests.get` re-probe (`backend/health_check.py`), so calling it bare on the event loop would be exactly the blocking-I/O violation `.agents/ENGINEERING_RULES.md` forbids. `record_success`/`record_failure` are wired **exactly one per attempt**, not one per outcome branch: `:618`/`:631`/`:650` (sync), `:1799`/`:1808` (Claude), `:1892`/`:1901` (async Gemini). The empty-response branch (`:631`) records a *failure*, not a success — the naive "the HTTP call returned, so the provider is up" reading would let an endless stream of empty completions reset the counter and keep the circuit permanently closed, which is the failure mode this whole item exists to prevent.

`_call_anthropic_httpx_model()` was restructured around an `_error_result()` closure so the circuit-open path preserves the `gemini_error: <upstream>; <ours>` prefix convention that the existing error-shape assertions depend on, rather than dropping the upstream cause on the floor.

**Deliberately not wired:** `summarize_with_gemini()` (used by `/api/bookmarks/semantic`) — a real Gemini call site, but not on `/ask`, and this item's scope is the `/ask` primary path. Noted here so its absence reads as a decision.

**Test evidence:** `tests/test_ask.py::TestAskPrimaryAiCircuitBreaker` (`:723-948`), 8 tests. Two are route-level and mirror `TestAskDegradationPath`'s structure across both transports (`test_flask_both_ai_circuits_open_skips_calls_and_uses_local_fallback` `:779`, `test_fastapi_..._local_fallback` `:814`); both drive `FAIL_THRESHOLD` failures into `gemini` *and* `claude`, then prove the negative with **spies rather than response assertions** — `_spy_on_provider_entrypoints()` (`:736`) monkeypatches `_configure_gemini_client`, `_generate_gemini_content_with_retry` and `_get_async_client` to *record* rather than raise (a raising spy would be swallowed by `/ask`'s own `except` and land on the identical fallback payload, making the test pass for the wrong reason), and asserts `attempts == []`; `_spy_on_local_fallback()` (`:766`) wraps — does not replace — `get_halakhic_sources` and asserts it was reached exactly once, with `meta.fallback is True`. The Flask test passes `environ_base={"REMOTE_ADDR": "10.39.26.1"}` because the shared 127.0.0.1 limiter bucket is already exhausted by earlier `/ask` tests in the same file — same per-IP trick `TestAskRateLimit` already uses. The remaining six are unit-level and pin the parts a route test can't see: exact circuit-open dict shape for both primaries (`:843`, `:860`), the preserved `"gemini_error: gemini_circuit_open; anthropic_circuit_open"` composite (`:872`), and the three record-call assertions including the empty-response case (`:888`, `:905`, `:928`). Full suite: **1431 passed**, coverage 91.53% (gate 85%); `backend/claude.py` line coverage 93%. No `/ask` response-shape change and no edit to any pre-existing fallback assertion.

### 26.2 The new `pa11y-ci` accessibility CI gate only exercises light theme — ✅ **Done 2026-08-20** (Prompt 39)

**Severity:** Medium. Not a currently-known accessibility defect — the existing manual pass in `docs/ACCESSIBILITY_AUDIT.md` already covers dark-theme contrast. But it means the *automated, CI-enforced* gate added by Prompt 18 has a coverage hole relative to what it appears to guarantee, and any future dark-theme-only regression (a token change, a new component styled only for one theme) would ship silently.

**Where:** `.pa11yci.json` (new this pass) and the "Accessibility scan" step in `.github/workflows/ci.yml`. `pa11y-ci` drives headless Chrome, which resolves `prefers-color-scheme` to `light` in the CI environment, and this app's own theme-detection script (checked against `localStorage`) also defaults to light with nothing set — so all 8 configured pages are checked once, in light theme only.

**Deliverable:** extend `.pa11yci.json` (or the CI step invoking it) to also check each page in dark theme — `pa11y`'s `actions` array supports injecting `localStorage`/a cookie before the check runs (e.g. `evaluate: () => localStorage.setItem('theme', 'dark')` followed by a reload action, matching however `templates/index.html`'s theme script actually reads its stored preference). Either double the page list with a `-dark` suffix per entry, or add a second `pa11y-ci` config/CI step scoped to dark theme.

**Exit criteria:** CI accessibility scan covers all 8 pages in both light and dark theme (16 checks total, or equivalent); 0 violations in both; `docs/ACCESSIBILITY_AUDIT.md` and `templates/accessibility.html` updated to note the automated gate now covers both themes, not just light.

**Resolved 2026-08-20 (Prompt 39) — and this one was not theoretical: the first dark run failed 16 elements immediately.** The deliverable above proposed doing this inside `.pa11yci.json`'s `actions` array. That turned out to be impossible, and the wrong assumption is worth recording: **pa11y's action grammar has no JS-evaluate action** (pa11y 9.1.1 — `screen capture`, `navigate to`, `click element`, `set field`, `check field`, `wait for …`; nothing that runs arbitrary script), so `evaluate: () => localStorage.setItem(...)` as written in the deliverable is not a thing pa11y can do. A second approach — emulating `prefers-color-scheme: dark` at the browser level (`--force-dark-mode`, `--blink-settings=preferredColorScheme=…`) — was tried and also fails, for an app-specific reason: a probe confirmed `prefersDark: true` while `document.documentElement.dataset.theme` was still `"light"`, because `templates/index.html`'s FOUC script normalizes an *absent* stored preference to `'light'` rather than deferring to the media query. Media-query emulation therefore cannot reach dark theme in this app at all.

What actually works is pa11y's documented `browser` + `page` + `ignoreUrl` options, and that is what `scripts/a11y_dark_scan.js` (new, 153 lines) uses: it resolves the same puppeteer pa11y itself resolves, seeds the real stored-preference key with `page.evaluateOnNewDocument` before first paint (`:72-81`), navigates, and only then hands the pre-warmed page to `pa11y(url, {...defaults, browser, page, ignoreUrl: true})` (`:103`). The key is `Sh'elahPrefs` (`:55`) — a JSON prefs blob whose `theme` field is `'light' | 'dark' | 'system'`, **not** a bare `theme` key as the deliverable guessed; the script reads-merges-writes rather than overwriting so it can't clobber unrelated prefs. It re-uses `.pa11yci.json`'s own `urls` and `defaults` (stripping `_`-prefixed comment keys) so there is exactly one URL list to maintain, and a **drift guard** at `:90` asserts `data-theme === 'dark'` after navigation and fails loudly naming the key if the app ever renames it — verified by temporarily renaming `THEME_PREFS_KEY` and confirming the guard fires with the intended message, then reverting. Without that guard a renamed key would silently turn the dark scan into a second light scan that always passes.

Wiring: `package.json:13-15` — `test:a11y` = `test:a11y:light && test:a11y:dark`, chained with `&&` so a light failure short-circuits (the first failure is the one to fix). `.github/workflows/ci.yml:85` renamed the step to "Accessibility scan (pa11y-ci, WCAG2AA — light + dark theme)"; the `run:` block itself is unchanged, still `npm run test:a11y`, so a red run stays **one** CI signal rather than two.

**The 16 violations it found, and the pattern behind them:** 15 at 4.34:1 (`--ink-secondary` `#888077` on `--surface-2` `#1e1d1a` — Popular Texts card subtitles, footer disclaimer) and 1 at 4.10:1 (`--ctrl-text` `#888070` on `--surface-pill` `#23211f` — topbar language button), all normal-weight sub-14px text with no large-text 3:1 exemption. This was the **third** lightening of `--ink-secondary`, and each prior pass had fixed only the single surface it happened to measure — §18's fix targeted `--surface-1`, and before that a light-theme pass declared dark "already passes" on the strength of `--surface-0` alone. So this pass computed the new values against **every** dark surface including hover: surface-0 6.30, surface-1 6.02, surface-2 5.43, surface-3 5.92, surface-pill 5.17, ctrl-bg-hover 4.54. `--ink-secondary` → `#999188`, `--ctrl-text` → `#999181`, each updated in all **three** places the dark palette is duplicated (`static/css/tokens.css:310`/`:336` under `[data-theme="dark"]`, `:528`/`:546` in the `prefers-color-scheme` fallback, `:594`/`:601` under the legacy `--text-muted`/`--control-text` aliases) — a grep for the old hexes now returns only comments. The full ratio table is written into the token comments at `:297` so the next person doesn't have to re-derive it. Cost, recorded honestly: the muted/primary separation narrows 2.02:1 → 1.61:1 — still plainly a muted tier, and preferable to a fourth round of clearing the threshold by hundredths.

Docs: `docs/ACCESSIBILITY_AUDIT.md` gained an "Automated coverage (CI)" subsection (`:21`), recomputed dark-theme rows for `--surface-1`/`--surface-2`/`--ctrl-text`-on-`--ctrl-bg`/`--ctrl-bg-hover`, a correction to finding 3's stale "dark already passed" claim, and a new finding 4 (`:123-145`) documenting both the violations and the fix-only-what-you-measured pattern. `templates/accessibility.html:100,102,118` now says "in both light and dark themes" in `data-en`, `data-he`, and the rendered fallback.

**Live verification (local, 8 pages × 2 themes = 16 checks): 8/8 light, 8/8 dark, 0 errors.** Local runs need `PUPPETEER_EXECUTABLE_PATH` pointed at a system Chrome (this machine has no `~/.cache/puppeteer` download); no machine-specific path was baked into any committed config — CI installs its own browser.

### 26.3 `ruff` reports lint errors concentrated in `tests/` — ✅ **Done 2026-08-20** (Prompt 39)

**Severity:** Low. `ruff` already runs in CI as a deliberately non-blocking (`continue-on-error: true`) advisory step, so this is hygiene debt, not a functional defect or a CI gate failure.

**Where:** `ruff check .` (run during this pass's Verify phase) reports errors concentrated in test files: `E402` (module-level import not at top of file), `F401` (unused import), `E741` (ambiguous variable name, e.g. `l`/`I`/`O`), and `E401` (multiple imports on one line).

**Deliverable:** a mechanical cleanup pass over the flagged test files — move imports to the top where a genuine ordering dependency doesn't require otherwise (some `E402` hits may be deliberate, e.g. a `sys.path` shim before an import; verify before moving), remove unused imports, rename ambiguous single-letter variables, split combined imports. No behavior change expected; run the full suite after to confirm.

**Exit criteria:** `ruff check .` reports zero errors (or a documented, narrowed `# noqa` for any deliberate exception); `pytest -q` still green; CI's `continue-on-error` on the ruff step can then be reconsidered (out of scope for this item — a separate decision about whether to make lint blocking).

**Resolved 2026-08-20 (Prompt 39): `ruff check .` → `All checks passed!`** Worth noting the premise was slightly off — the errors were *not* concentrated in `tests/`; the two largest clusters were `app.py` (19 E402) and `asgi.py` (19 E402), and `tests/` cleared with four one-line import moves. There is no `pyproject.toml`/`ruff.toml` in this repo, so this is ruff's default rule set (E4, E7, E9, F).

Genuine fixes, no suppression: `tests/test_calendar_service.py`, `tests/test_search.py`, `tests/test_search_provider.py`, `tests/test_zmanim_engine_extra.py` each had a mid-file `from backend.health_check import FAIL_THRESHOLD` hoisted to the top import block (each was checked for an ordering dependency first — none had one). `backend/routes_prayers.py:71,73` E741: the ambiguous `l` in both preview comprehensions renamed to `line`. `scripts/crawl_library_leaves.py`: unused `Optional` dropped from its `typing` import.

Two structural fixes did most of the work, and both were real code-organization bugs rather than lint noise. `app.py`: a stray `_THREAD_POOL = ThreadPoolExecutor(max_workers=8)` sat *inside* the import block, so ruff flagged every import after it — moved below the imports (with a comment citing this section), dropping app.py 19 → 5. `asgi.py`: identically, `_RateLimitStore = collections.OrderedDict` and `logger = logging.getLogger(__name__)` sat between the stdlib and third-party import groups; moving both below the import block cleared all 19 with **zero** `# noqa`. That move was made carefully — `asgi.py` carries a load-bearing INIT-ORDERING comment on `from backend.logging_setup import ...` (Sentry's module-level `init()` must run before `FastAPI(...)` is constructed); the import itself was not touched and the comment is preserved verbatim. Verified by smoke-importing the module and confirming both names still resolve.

Five deliberate exceptions remain, all in `app.py`, each a **narrow per-line `# noqa: E402` with a one-line reason** (no file-level or rule-level suppression anywhere): `:911` `from backend.rag import _env_int` and `:1106` `from backend.rag import (...)` — back-compat re-export shims that must stay beside the config block / at the bottom; `:921` `from backend.auth import (...)` — grouped with the Supabase/auth helpers it serves; `:2097` `import sys as _sys` — the `__main__`→`app` alias must be installed after the app object exists; `:2116` `import importlib as _importlib` — blueprint registration must follow app construction. All five are the pre-existing deliberate structure `app.py`'s own comments already explain; the `# noqa` just makes the intent machine-readable.

Full suite re-run after every edit in this item: **1431 passed**, 91.53% coverage, zero behavior change. CI's `continue-on-error: true` on the ruff step was left as-is, per this item's own exit criteria.

### 26.4 Ordering

26.3 is the cheapest and most mechanical — safe to do any time as a standalone pass. 26.1 and 26.2 are both medium-severity, independent of each other and of 26.3, and can be done in either order; 26.1 sits closer to the reliability work in §8.E/§17, 26.2 closer to the quality work in §8.F/§18.

---

## 27. Findings from implementing Prompt 20 (§9 agentic tool-use layer) (found 2026-08-21)

Prompt 20 (`claude_code_prompts.md`) implemented the agentic tool-use layer in full — `backend/ai_tools.py` (22 tools), `backend/ask_pipeline.py::run_agentic_ask`, `backend/claude.py::_call_anthropic_agentic_turn`, the `AI_AGENTIC_TOOLS` flag, `docs/AI_TOOLS.md`, and the three test files listed in §9.6 — with `pytest -q` green with the flag both unset and `=true`. Two genuine bugs surfaced *in code written for this same deliverable* (not pre-existing app bugs) were fixed directly during implementation rather than deferred here: `text_engine.format_source_citation`'s citation-splitting logic broke for any multi-word book title, and `ai_tools.execute_tool()` called the circuit-breaker's `is_healthy()` bare inside an async function (a blocking-I/O-on-the-event-loop violation, since `is_healthy()` can perform an inline blocking re-probe) — see `docs/AI_TOOLS.md`'s "Implementation notes" for both. This section catalogs everything else found along the way that is genuinely out of Prompt 20's own scope: two pre-existing bugs in code the new tools now call live for the first time, one planning-document tension this prompt's own completion resolves the premise of, and two catalog-accuracy gaps in plan.md's own tool tables. None of these block Prompt 20's own acceptance bar, which is met; all are follow-up.

### 27.1 `backend/search.py`'s Wikipedia connectors send no `User-Agent` header — Wikimedia API policy risk, now live on the last-resort `web_search` tool

**Severity:** Medium. This code predates Prompt 20, but Prompt 20's `web_search` tool (`backend/ai_tools.py`) is the first thing that calls `async_search_wikipedia` from a live, user-facing, model-invoked path rather than the older best-effort general-web fallback — so a pre-existing latent issue is now on a path with real traffic once `AI_AGENTIC_TOOLS=true` ships.

**Where:** `backend/search.py:36-43` (`_get_async_client`) constructs the shared `httpx.AsyncClient(timeout=10.0)` with no default `headers=`; `backend/search.py:231-261` (`async_search_wikipedia`) issues `client.get(url)` with no per-call headers either. The synchronous twin has the identical gap: `backend/search.py:25` (`_HTTP = requests.Session()`) is never followed by a `.headers.update(...)` call anywhere in the file, and `search_wikipedia` (`:46-74`) calls `_HTTP.get(url, timeout=10)` with no headers. Both therefore send whichever default `User-Agent` string httpx/requests generates (`python-httpx/x.x.x` / `python-requests/x.x.x`). This is a real behavioral gap, not a style nit: Wikimedia's REST API (`en.wikipedia.org/api/rest_v1/...`) documents a User-Agent policy and is known to 403/429 default-library User-Agent strings for meaningful traffic — this was directly observed (an actual `403`) during this session's own smoke-testing of the new `web_search` tool against the live API, before the respx-mocked test suite was written. Contrast with `backend/search.py:214-215` and `:348-349`, where two *other* connectors in the same file already set an explicit `"User-Agent": "Mozilla/5.0 (compatible; ShelahBot/1.0; +https://www.sefaria.org)"` — so the fix pattern already exists in this exact file, just not applied to the two Wikipedia functions.

**Deliverable:** add the same `User-Agent` header (or a dedicated one identifying the app per Wikimedia's actual policy, which asks for a contact URL/email — `https://meta.wikimedia.org/wiki/User-Agent_policy`) to `_get_async_client()`'s `httpx.AsyncClient(...)` construction (covers `async_search_wikipedia` and any future user of the shared async client) and to `_HTTP`'s session in `search_wikipedia`'s sync path, matching `:214-215`'s existing precedent exactly rather than inventing a new string.

**Exit criteria:** a live (or precisely mocked, asserting the request headers) call to `async_search_wikipedia`/`search_wikipedia` sends a descriptive `User-Agent`; a manual/live smoke test against the real Wikipedia API no longer 403s for a query that previously did; no change to either function's return shape or caching behavior.

### 27.2 `zmanim_engine.get_monthly_events` builds the Hebcal URL from the raw `timezone_str` parameter, not the resolved timezone — a `None` timezone silently degrades the request

**Severity:** Medium. Not a new bug introduced by Prompt 20, but now directly reachable through two of the new tools (`get_holidays`, `get_daily_zmanim_summary`) whenever the agentic loop's `tool_context` lacks a resolved timezone — which `run_agentic_ask`'s own docstring already documents as a real, expected case ("location required" rather than guessing), making this more likely to actually fire than it was pre-Prompt-20.

**Where:** `backend/zmanim_engine.py:445` — `def get_monthly_events(lat, lon, timezone_str=None):`. Line 451 correctly resolves the timezone through `_resolve_timezone(lat, lon, timezone_str)` into a local `tz_name`, and that resolved value is what's used to build the `GeoLocation` object on line 452. But the Hebcal URL built at lines 480-488 uses the **raw, unresolved parameter** at line 486 — `f"&tzid={timezone_str}"` — not `tz_name`. When a caller omits `timezone_str` (the function's own documented default), the URL literally becomes `...&tzid=None`, sent to Hebcal's live API. Whether Hebcal's API rejects that outright (triggering the `except Exception` at `:495-497`, which only `print()`s and silently returns whatever solar-only events were already gathered) or accepts it and returns data for an unintended default timezone, the caller gets wrong or silently-incomplete holiday/candle-lighting data with no signal that anything went wrong — the function still returns `200`-shaped success (`events`, populated at least with the 30 days of solar events from step 1, which never depended on `timezone_str`).

**Deliverable:** change line 486 to use the already-resolved `tz_name` instead of the raw `timezone_str` parameter — the one-line fix `f"&tzid={tz_name}"`. Add a regression test calling `get_monthly_events(lat, lon, timezone_str=None)` (mocking the Hebcal HTTP call) and asserting the outgoing request's `tzid` query parameter is never the literal string `"None"`.

**Exit criteria:** `get_monthly_events` sends a real resolved `tzid` to Hebcal even when called with `timezone_str=None`; new regression test passes; no change to the function's other behavior (solar events, caching key, holiday item shape).

### 27.3 Prompt 35's recommendation to delete `backend/ask_pipeline.py` is now half-stale — Prompt 20 made part of it live

**Severity:** High (planning-document integrity, not a code defect) — this is exactly the kind of "four documents describing the same thing as undecided" pattern §23/Prompt 36 already flagged as a failure mode elsewhere in this project, now recurring in a new spot.

**Where:** `claude_code_prompts.md` Prompt 35 (§22) recommended, as of its 2026-08-19 analysis, deleting `backend/ask_pipeline.py` outright (`git rm`, plus its two dedicated test files) on the grounds that it was "515 lines with zero production importers." That diagnosis was correct *at the time*. Prompt 35's own text anticipated this exact situation, saying explicitly: "Leave §9 / Prompt 20 as the place a future shared pipeline would live." Prompt 20 has now done exactly that — `ask_pipeline.py`'s new `run_agentic_ask()` function is a genuine production importer: `app.py`'s `_run_ask_question_ai_synthesis` and `asgi.py`'s `_run_ask_async_ai_synthesis` both call it directly whenever `AI_AGENTIC_TOOLS=true`. The module is no longer uniformly dead — but it is not uniformly live either: the **other half** of the same file, the pre-existing `run_ask_pipeline`/`AskPipelineResult` exports that Prompt 35 was actually describing, remain exactly as dead as Prompt 35 found them (their only importers are still their own two dedicated test files, `tests/test_ask_pipeline.py` and `tests/test_ask_pipeline_smoke.py`). Prompt 35 itself has not been executed — no deletion has happened — so this is not yet a conflict in the code, only a now-inaccurate premise in Prompt 35's written recommendation and in the roadmap ordering note (`claude_code_prompts.md`'s "Suggested execution order" table, row 6b: "Still resolve before or alongside 20, since 20 is the only roadmap item naming this file as a wire-in target").

**Deliverable (for the next pass, likely combined with or superseding Prompt 35's own STEP 1-4):** re-scope Prompt 35 to a **split decision** rather than a whole-file deletion: (a) delete only the now-confirmed-still-dead `run_ask_pipeline`/`AskPipelineResult` code and its two dedicated test files — Prompt 35's STEP 1 diagnosis is still correct for this half; (b) keep the module itself and its new `run_agentic_ask` export, since it is production-reachable behind the flag; (c) Prompt 35's STEP 2 (`tests/test_ask_transport_parity.py`, pinning the Flask/ASGI `/ask` handlers to each other on response-key-set, retry/timeout behavior, prompt-selection thresholds, `classify_safety()` invocation, per-user budget check, and meta key set) is still a fully valid, still-unbuilt deliverable, independent of the deletion question — build it regardless. Update Prompt 35's own text, `plan.md` §22, and this row's cross-reference in the same commit so this doesn't become a fifth document describing the file as undecided.

**Exit criteria:** `backend/ask_pipeline.py` contains only `run_agentic_ask` and its direct helpers (no dead `run_ask_pipeline`/`AskPipelineResult` code); `tests/test_ask_transport_parity.py` exists and covers Prompt 35 STEP 2's six invariants; `plan.md` §22 and Prompt 35's text both reflect the module's actual current split state rather than either "fully dead" or "fully undecided."

### 27.4 plan.md §9.2b named two backing functions that live in `app.py`, which `backend/ai_tools.py` cannot import — a tool-catalog documentation drift, not a code gap

**Severity:** Low — the code is correct and covered by tests; this is purely about `plan.md`'s own table text no longer matching what was actually built, for a documented and deliberate reason.

**Where:** `plan.md` §9.2b's table names `get_community_profile` as backing onto "community routes data (`/api/community/<name>`, timeline)" and `get_prayer_text` as backing onto `routes_prayers` (`get_prayer`, `get_siddur_full`) / `_get_prayer_refs`. `_get_prayer_refs` and the siddur-section mapping it names live in `app.py`, not `backend/`. Prompt 20's own instruction (`claude_code_prompts.md` Prompt 20, point 1) requires `backend/ai_tools.py` to import `backend.*` only, never `app` — a hard constraint, since the registry must be callable from any transport and from tests without booting Flask. Both tools were therefore implemented as independent, backend-only reimplementations rather than thin wrappers around the named functions — a deliberate, correct call given the constraint, documented in each handler's docstring and in `docs/AI_TOOLS.md`'s "Implementation notes," but it leaves `plan.md`'s own table asserting a backing relationship that isn't quite what was built.

**Deliverable:** update `plan.md` §9.2b's two table rows for `get_community_profile` and `get_prayer_text` to describe the actual backend-only data sources each handler uses (see `backend/ai_tools.py`'s handler docstrings for the specifics), with a one-line note that this differs from the original table because the originally-named functions are `app.py`-only.

**Exit criteria:** `plan.md` §9.2b's table text matches `backend/ai_tools.py`'s actual implementation for both rows; no code change required.

### 27.5 Gemini agentic follow-up (scoping note, not a defect)

**Severity:** Low / product decision, not a bug. `_call_anthropic_agentic_turn` is Anthropic-only by deliberate scope decision (see `docs/AI_TOOLS.md`'s "Implementation notes" #3) — the pre-fetch RAG path this layer sits behind is multi-model (Gemini primary, Claude fallback), so when `AI_AGENTIC_TOOLS=true`, every agentic answer currently comes from Claude regardless of the non-agentic path's Gemini-primary preference. This is a legitimate, cost/complexity-aware scoping call for a first pass (Gemini's function-calling request/response shape differs and would need its own parsing branch in a function structurally parallel to `_call_anthropic_agentic_turn`), not an oversight — flagged here only so it's a named, deliberate gap rather than a silent one if/when the flag is ever considered for default-on.

### 27.6 Ordering

27.1 and 27.2 are both small, independent, one-file fixes with a clear regression test each — do either any time, in either order. 27.3 is the one item here with real sequencing weight: it should be resolved *before or alongside* whatever pass finally executes Prompt 35, since it changes what "delete `ask_pipeline.py`" now actually means — read this section first if picking Prompt 35 back up. 27.4 is documentation-only and can be folded into whichever pass touches `plan.md` §9 next. 27.5 requires no action unless/until Gemini-primary agentic mode becomes a real product ask.

---

## 28. Findings from implementing Prompt 36 (§23 schema & documentation provenance) (found 2026-08-21)

Prompt 36 closed §23.2.1 (provenance headers), §23.2.4 (the swallow-bug audit), and §23.2.5/§23.3 (dangling-citation repoint + the new `TestPlanMdCitationsResolve` lint) — see that section's updated status for full evidence. §23.2.3 (the live schema-doc generator) was built and confirmed reachable against the real Supabase project, but not fully executed — that gap is already tracked as an open exit criterion in §23 itself, not repeated here. This section catalogs two things found *while* doing that work that are outside Prompt 36's own scope.

### 28.1 The entire repo has ~150 modified/untracked files sitting uncommitted since 2026-08-20 — a real loss-of-work and unreviewable-diff risk

**Severity:** High (process/operational risk, not a code defect). This is not a new observation about any single file — it is a fact about the repository's current state that no prior section names directly, and it changes how every other open item in this document should be read: a large fraction of what the status tables above call "✅ Done" exists **only in this local working tree**, not in git history.

**Where:** `git log -1` shows the last real commit as `97da76d` (2026-08-20, "Add missing base CREATE TABLE for ai_usage_log"). `git status --porcelain` today shows 97 modified files, 53 untracked files, and a handful of staged-but-uncommitted adds/deletes — roughly 150 paths — spanning nearly every backend module, every CSS/JS asset, `templates/`, `docs/`, `scripts/`, both root planning documents (`plan.md`, `claude_code_prompts.md`), and the `graphify-out/` graph itself. Cross-referencing against the status table in `claude_code_prompts.md`, this single uncommitted mass appears to represent the accumulated work of **many** prompts — everything from Prompt 12 (§8.A legal docs) forward, including Prompts 20/21/22 marked "completed 2026-08-21" (today) in that same table. None of it has a commit boundary, a commit message, or a code-review-sized diff.

**Why this matters beyond tidiness:** (a) it is a single-point-of-failure — this only exists on one machine's local filesystem; a bad `git clean -fdx`, a `git checkout -- .` run without checking `git status` first, or simply a disk failure would lose weeks of work with no recovery path; (b) it defeats the entire point of this project's own "golden-master tests before moves" / "verified before completion" discipline (`.agents/ENGINEERING_RULES.md`) — that discipline assumes each verified increment is *captured*, not left to accumulate indefinitely; (c) it makes `git bisect`/`git blame` useless for anything in this range, which matters for a project that has already shipped at least one silent production bug (§23.1) that a later session had to rediscover by reading code rather than history; (d) every "Done" claim in the status tables is currently unfalsifiable against git history — there is no commit to point at that proves a given prompt's changes are what actually shipped, only this session's own re-reading of the working tree.

**Deliverable:** not a code change — a housekeeping pass, ideally done by (or with direct confirmation from) the repo owner rather than autonomously, since bundling ~150 files into commits is itself a judgment call about grouping and commit messages that benefits from a human's sign-off. Recommended approach: commit in the same increments the status table already describes (roughly one commit per completed Prompt row, oldest-first, using each row's own evidence paragraph as the commit-message basis), rather than one giant commit — this preserves the ability to `git log`/`bisect` per-prompt going forward, which is exactly what §23 is trying to establish for schema changes and should apply here too. A single `git add -A && git commit` would technically stop the *growth* of this risk but would forfeit the per-prompt history that makes the rest of this recommendation worth doing at all.

**Exit criteria:** `git status --porcelain` is clean (or close to it) against a commit history where each commit corresponds to a reviewable, named increment of work; going forward, each daily/scheduled implementation pass ends with its own commit rather than adding to an ever-growing uncommitted pile.

### 28.2 `scripts/sql/rag_identity_cache_setup.sql`'s filename is itself doc-drift — it doesn't create a `rag_identity_cache` table

**Severity:** Low — cosmetic/naming, not a functional bug; already called out in `DECISIONS.md` and now in `docs/DATABASE.md`'s regenerated prose, but the misleading filename itself was left unchanged by this pass.

**Where:** `scripts/sql/rag_identity_cache_setup.sql` actually creates `community_knowledge` and `user_memories` — there is no `rag_identity_cache` table anywhere in this schema (confirmed again while writing this pass's `docs/DATABASE.md` reconstruction). The filename is referenced in prose (not executed by name) in four places: `docs/PRIVACY_OPERATIONS.md:51`, `backend/routes_privacy.py:19` (a comment), `docs/DATABASE.md` (this pass's own new text, which explicitly notes the mismatch), and `scripts/README.md:33`.

**Deliverable:** rename to something accurate (e.g. `community_knowledge_and_user_memories_setup.sql`) and update the four prose references above. Low risk — nothing imports or invokes this file by name programmatically (it is hand-run in the Supabase SQL editor like every other file in `scripts/sql/`), so the rename cannot break a running system, only stale prose if a reference is missed.

**Exit criteria:** no file in the repo is named for a table it doesn't create; the four cross-references point at the new filename; `docs/DATABASE.md`'s base-table link for `community_knowledge`/`user_memories` resolves correctly.

### 28.3 Ordering

28.1 is the one worth acting on first and is explicitly **not** a task for an autonomous pass to execute unilaterally — it needs the repo owner's judgment on commit grouping, or at minimum their go-ahead before an agent commits ~150 files on their behalf. 28.2 is small, independent, and safe to fold into any future pass that touches `docs/DATABASE.md` or `scripts/sql/` again — including a real (non-interim) run of §23.2.3's generator, which will need to point at the renamed file too.

---

## 29. Findings from implementing Prompt 25 (§12 Phase 6 product surface, content & growth layer) (found 2026-08-21)

Prompt 25 shipped §12.1 (About page), §12.2 (Help page + glossary page reusing the existing static glossary data), §12.3.1 (reader breadcrumbs), §12.3.3 (confirmed citation deep-linking already worked — no code needed), §12.3.4 (a circuit breaker around `_retrieve_community_knowledge`), §12.4 (an end-to-end answer-feedback loop: route, migration, devtools digest, frontend widget), and §12.5.2-§12.5.4 (JSON-LD additions, a consent-gated Vercel Web Analytics loader, `robots.txt`/`sitemap.xml` as Flask routes) — full `pytest -q` green (91%+ coverage, no regressions), and verified against a real local dev server with a real signed-in Clerk session and a real Supabase project. §12.3.2 (the library search filter UI) was deliberately **not** built this pass — a scoped, reasoned cut, not an oversight, detailed in §29.1. This section catalogs seven items found along the way that sit outside a strict reading of Prompt 25's own acceptance bar: one descoped sub-item, one planning-document/live-config contradiction this pass could not resolve without a live deploy, one missing content page the spec assumed exists, three already-shipped-before-this-pass corrections to plan.md's own text, one known counting limitation in new code, and one unrelated live production bug found incidentally while browser-verifying this pass's own work. None of these block Prompt 25's own bar, which is met; all are follow-up.

### 29.1 The library search filter UI (§12.3.2) was deliberately descoped, not skipped by oversight

**Severity:** Medium (product gap, not a defect). The backing state already exists and is already wired end-to-end on both sides — only a control surface is missing.

**Where:** `backend/search.py`'s `search_library(query, filters)` and the frontend's `appState.prefs.metadataFilters` / `normalizeMetadataFilters()` / `appendMetadataFilters()` (`templates/index.html`) already accept and forward `era`/`author`/`category`/`geography`/`nusach` filters — confirmed by direct code reading, not assumption. Zero UI control writes into that state. The topbar search area (`templates/index.html` reader header, search input + suggestions dropdown + settings menu) is already visually dense with multiple overlapping z-index layers; this pass judged that adding an unverified filter panel into it — without budget left in the same pass to check it across mobile/RTL/dark-mode, per `.agents/ENGINEERING_RULES.md`'s responsiveness/contrast/touch-target bar — was a worse trade than shipping the rest of §12 cleanly and flagging this gap explicitly.

**Deliverable:** design and build a filter control (collapsible panel, or a dedicated filter row on a `/library` view — implementer's call) that writes into `appState.prefs.metadataFilters` and triggers the existing search-call/`pushPrefsToServer()` path the same way the state is already consumed. Free-text inputs are fine for `author`/`geography`/`nusach` (no fixed taxonomy exists); `category` may be a dropdown if `get_library_index()`'s category tree is cheap to expose as options.

**Exit criteria:** a real filter control exists, writes verified into `appState.prefs.metadataFilters`, and search results visibly narrow when a filter is set; verified in light + dark theme, 375px mobile viewport, Hebrew/RTL mode, keyboard-only operation, and 44×44px touch targets.

### 29.2 `vercel.json` has no `rewrites` array — contradicts plan.md §11/§14.3.2's premise, and this pass deliberately did not touch it

**Severity:** High (planning-document/production-config integrity — the kind of contradiction §23/Prompt 36 already flagged as a recurring failure mode elsewhere in this project).

**Where:** `plan.md` §11 and §14.3.2 both assert a rewrite with a negative-lookahead regex already excludes `static/`, `favicon.ico`, `manifest.webmanifest`, `service-worker.js` from being routed to the Python serverless function, and §12.5.1 assumed this same mechanism could simply be extended to cover the new `robots.txt`/`sitemap.xml` routes. A fresh, direct read of the live `vercel.json` during this pass shows it contains only `$schema`, `functions` (`api/index.py`, `maxDuration: 90`), `regions` (`["iad1"]`), `crons` (2 entries), and `headers` (1 entry, `/static/(.*)` cache-control) — **no `rewrites` key at all**. Compounding this, `favicon.ico`/`manifest.webmanifest`/`service-worker.js` are themselves Flask routes in `app.py` (lines ~1434-1449), not physical files at the repo root — so even if the described rewrite existed, these three paths would still need to reach the Python function (or 404), which is internally inconsistent with §11's own framing of them as rewrite-excluded static assets. This pass could not resolve which premise is wrong (the rewrite was never actually added; or it exists via some other project-level Vercel setting outside this file; or routing "just works" today for a reason not yet identified) because the Vercel CLI hangs/infinite-loops in this sandbox (`vercel --version`/`vercel whoami` never terminated), and there is no way to verify live Vercel routing behavior without either a working CLI session or a real preview deploy — both are Prompt 24 prerequisites, not something available to this pass.

**Deliverable:** given the uncertainty, this pass deliberately did **not** modify `vercel.json`. Instead, `robots.txt`/`sitemap.xml` were added as ordinary Flask routes in `backend/routes_pages.py`, matching the existing favicon/manifest/service-worker precedent exactly — so they inherit whatever routing behavior those three already have in production, correct or not, rather than depending on an unverified rewrite. Once Prompt 24's live-deploy verification lands (or is run alongside this), determine the real routing behavior, then either add the negative-lookahead rewrite for the first time (if it was never actually present) or correct §11/§14.3.2's text to describe reality (if routing works today via some other mechanism).

**Exit criteria:** `plan.md` §11 and §14.3.2 describe `vercel.json`'s actual, verified-live routing behavior; if a rewrite is added, it is its own reviewed change with its own before/after verification against a real preview deploy — not silently bundled into an unrelated commit.

### 29.3 The sitemap spec assumes a "parasha page" that doesn't exist as an HTML route

**Severity:** Low-Medium (content/IA gap). Already correctly handled defensively — `_SITEMAP_PATHS` simply omits it rather than pointing at a JSON endpoint — but the underlying page itself is still missing.

**Where:** `plan.md` §12.5.1 lists a parasha page among the sitemap's stable public routes. The only parasha-related route in the codebase is `backend/routes_calendar.py`'s `GET /api/parasha` — JSON only, not a crawlable HTML page. `backend/routes_pages.py::sitemap_xml`'s `_SITEMAP_PATHS` list currently excludes it for exactly this reason (confirmed no such page exists, rather than assuming plan.md's premise was correct).

**Deliverable:** either build a real crawlable parasha page — a genuine content/IA decision, not a small template change, since a stable permalink needs a per-parasha-name slug scheme rather than a "this week" URL that goes stale — or formally drop the claim from §12.5.1's text. Do not add a placeholder sitemap entry pointing at the JSON endpoint just to satisfy the list.

**Exit criteria:** either a real `/parasha/<slug>`-style HTML page exists and is added to `_SITEMAP_PATHS`, or §12.5.1's text is corrected to not claim one.

### 29.4 Three plan.md §12 claims were already shipped before this pass — documentation drift, not code gaps

**Severity:** Low (documentation-only; each item below is a correction to plan.md's own text, not a code change).

**Where:**
- §12.3.3 framed citation deep-linking as "net-new (small)" work. `templates/index.html`'s AI-source-box delegated click listener already called `readText(ref, {skipNavigationGrid:true})` on `a.source-local-link` clicks before this pass touched anything — confirmed by direct code reading. No new code landed here.
- §12.5.2 (and §12.0's claim-triage table) asserted no JSON-LD existed on the site. A `WebApplication` JSON-LD block already existed in `templates/index.html`'s `<head>`. This pass added `WebSite` and `Organization` blocks alongside it, not in place of it.
- §12.3.1 framed breadcrumbs as needing new backend work. `backend/sefaria_library.py::get_text`/`_parse_v3_response` already returned a `categories` field on every `/api/text/<ref>` response before this pass — the breadcrumb work that landed was frontend-only (a `nav[aria-label="breadcrumb"]` wrapper reusing the existing JS write-target).

**Deliverable:** update `plan.md` §12.0's claim-triage table and §12.3's numbered list to mark these three as already-shipped-before-Prompt-25, consistent with this project's own stated discipline (§12's own intro) of recording stale/incorrect claims rather than re-planning around them.

**Exit criteria:** §12.0 and §12.3's text in `plan.md` match what the code actually required for these three items.

### 29.5 The feedback widget's immediate-submit-on-click design causes a known double-count for commented 👎 responses

**Severity:** Low-Medium (data-quality limitation, not a user-facing bug — flagged now so it isn't rediscovered as a mystery later).

**Where:** `templates/index.html`'s `renderFeedbackWidget()` submits the bare verdict to `POST /api/feedback` immediately on click (both 👍 and 👎), specifically so a reader who clicks 👎 but never fills in the optional comment box still has their verdict recorded rather than losing it to comment abandonment — a deliberate tradeoff made during this pass's own design review (an initial draft that only submitted on "Send feedback" would have silently dropped every uncommented 👎). If the reader *does* then submit a comment, a **second** row is inserted with the same verdict plus the comment. `backend/routes_devtools.py::feedback_digest`'s `helpful`/`not_helpful` counts are therefore inflated by exactly one for every 👎 that gets a followed-through comment.

**Deliverable:** a real fix needs the first insert to return its row id (Supabase's default `Prefer: return=representation` already provides this for free) and a second endpoint that PATCHes that row's `comment` field in place, instead of inserting twice. Until built, `feedback_digest`'s response (or the devtools view consuming it) should at minimum note the skew in its own text.

**Exit criteria:** either a PATCH-based single-row flow replaces the double-insert (and `feedback_digest`'s counts are exact), or the known skew is documented at the point the digest is displayed.

### 29.6 Incidental finding, unrelated to §12: `/api/user/preferences` throws a live UUID type-mismatch error for real signed-in users

**Severity:** High (live production bug, currently broken for every real user) — found entirely incidentally while browser-verifying this pass's own §12 changes against the real dev server, not part of Prompt 25's scope.

**Where:** `backend/routes_user.py` (pre-existing code, untouched by this pass) — `GET /api/user/preferences` threw a PostgREST `22P02: invalid input syntax for type uuid: "user_3DJ2PONd1x9zBnfVRlfiGhMzQPr"` against the real Supabase project with a real signed-in Clerk session. The live `user_preferences.user_id` column is typed `uuid`, but Clerk `sub` claims are non-UUID strings like `user_3DJ2PONd1x9zBnfVRlfiGhMzQPr` — never valid UUIDs. This means preferences sync is currently broken in production for every real signed-in user, independent of anything in §12.

**Deliverable:** root-cause and fix — most likely the column should be `TEXT`, matching `ask_history.user_id` and the new `answer_feedback.user_id` (both correctly typed `TEXT` for this exact reason). Check whether `user_preferences` is the only table with this mistyped column, since it likely points at a copy-paste error in whichever migration created it, and audit for siblings. **Update (2026-08-21):** the fix already exists — `scripts/migrate_user_preferences_user_id_to_text.sql` (untracked-provenance file surfaced by §23.2.1) does exactly this `ALTER COLUMN user_id TYPE text`, idempotent either way. It has not been run against the real project. No new code needed — just run it (see §29.7's same "apply the migration" ask).

**Update (2026-08-22):** the column-type migration has since been applied — direct reads of `user_preferences` via both the service-role and anon keys with a fabricated non-UUID `user_id` now succeed, confirming the column itself is `text`. But live re-verification (real dev server, real signed-in Clerk session, the exact `user_3DJ2PONd1x9zBnfVRlfiGhMzQPr` id from the original report) still reproduces the identical `22P02` error on every request, ruling out schema-cache lag. Root cause: `migrate_user_preferences_user_id_to_text.sql`'s own strategy of discovering and preserving whatever RLS policies were live carried forward a stale, untracked, unnamed policy ("Users can manage their own preferences" — first surfaced in §30's own 2026-08-21 update) whose clause still casts `user_id` to `uuid`, unchanged from before the column-type fix. Because RLS-protected tables are enforced as security barriers, every applicable permissive policy must be evaluated for a real matching row, so this one stale policy alone 500s every authenticated call, even though unauthenticated/service-role reads never hit it. Fix written: `scripts/migrate_user_preferences_fix_rls_policies.sql` drops every non-canonical policy on the table and (re)creates the canonical four from `scripts/sql/bookmarks_and_preferences_setup.sql` (`auth.uid()::text = user_id`, safe regardless of the column's prior type). Not yet run against the real project — no DB credentials/CLI path from this sandbox, same constraint as elsewhere in §29/§30. Two regression tests were added to `tests/test_routes_user.py` pinning the raw (non-UUID) Clerk `sub` string through both the GET filter and the PUT upsert payload; full suite green (91%+ coverage). A DB-level regression test isn't possible from mocks — this class of bug (a stale RLS policy clause) only reproduces against the real, RLS-enforcing Postgres instance.

**Exit criteria:** `scripts/migrate_user_preferences_fix_rls_policies.sql` is run against the real project; `GET`/`PUT /api/user/preferences` work end-to-end for a real signed-in user against the real project (re-verify with the same live-browser method used to find this).

### 29.7 `answer_feedback` migration has not been applied to the real project; its RLS policy has not been verified live

**Severity:** Medium (currently-broken path with a known cause + an unverified security assumption, both from this pass's own new code).

**Where:** `scripts/migrate_answer_feedback.sql` (added by this pass) has not been run against the real Supabase project — confirmed via live browser testing: `POST /api/feedback` currently 500s with `PGRST205: Could not find the table 'public.answer_feedback' in the schema cache`. The route's own error handling worked correctly (caught, logged, clean JSON 500) and the frontend degrades gracefully (fire-and-forget submit, so the UI still shows a thank-you message) — this is a missing-migration issue, not a code defect. Separately, the migration's RLS design (one INSERT-only policy, `WITH CHECK (true)`, deliberately no SELECT policy so only the backend's secret-key client can read rows) was asserted correct by design but — matching this project's own already-flagged §21 gap ("verify RLS is real, don't just assert it") — has never been checked against a live, RLS-enforcing Postgres instance.

**Deliverable:** apply `scripts/migrate_answer_feedback.sql` to the real project (a "what the user needs to do" item — no DB credentials/CLI path exists from this sandbox). Once applied, confirm live that no `anon`/`authenticated` role can `SELECT` from `answer_feedback`, and that an INSERT succeeds for both anonymous and signed-in callers, using the same acceptance-test approach §21 already calls for elsewhere.

**Exit criteria:** the migration is applied; `POST /api/feedback` succeeds end-to-end against the real project; a live RLS check confirms no read access outside the backend's secret-key client.

### 29.8 Ordering

29.6 is the highest-priority item in this section — it is a live, currently-broken path for real users, unrelated to and unblocked by anything else here, and should be picked up first regardless of what else from this list is scheduled. 29.7's migration-apply half is a prerequisite for the feedback loop to work at all in production and has no dependencies; its RLS-verification half can happen anytime after. 29.2 was blocked on Prompt 24's live-deploy verification landing — that landed 2026-08-21 (§11's update note), so 29.2 is unblocked, but its actual question (does the live `vercel.json`'s missing `rewrites` key matter) was not specifically re-checked during that deploy and remains open — do not attempt to resolve it from static reading alone again. 29.1 and 29.3 are independent product/content decisions with no code dependencies on each other or on anything else in this section. 29.4 is documentation-only and can be folded into whichever pass next touches `plan.md` §12. 29.5 is small and independent; safe to defer indefinitely if the double-count skew is judged acceptable, but should at least be documented once picked up.

---

## 30. Findings from a live Supabase Advisor scan (found 2026-08-21)

The operator ran Supabase's built-in database linter/advisor against the real project and pasted its output. Every finding was cross-checked against the actual SQL files in `scripts/` before any fix was proposed — several of the advisor's warnings turned out to be intentional-by-design (already documented elsewhere in this repo), not defects. `scripts/migrate_security_hardening.sql` closes the three that are unambiguous and safe to fix without live-DB access; the rest need either a live read-only query the operator can run in seconds, or are already the exact subject of an open decision elsewhere in this document — this section does not re-litigate those, only points at the new evidence.

### 30.1–30.3 Fixed via `scripts/migrate_security_hardening.sql` (run this once you're set up)

Three advisor findings had unambiguous, safe fixes and are already written as a migration, following this project's established pattern (`scripts/migrate_answer_feedback.sql` etc.) rather than being applied live from this sandbox (no DB credentials/CLI path here, same constraint noted throughout §29):

- **`function_search_path_mutable`** on `public.set_updated_at_timestamp` and `public.check_and_reserve_user_budget` — both already fully schema-qualify every non-builtin reference they make, so `SET search_path = ''` (the strictest available fix) is safe for both.
- **`extension_in_public`** on `pg_trgm` — moved to a dedicated `extensions` schema via `ALTER EXTENSION pg_trgm SET SCHEMA extensions`, the documented Supabase remediation. Safe: the two GIN trigram indexes in `scripts/sql/rag_identity_cache_setup.sql` bind their operator class by OID at build time, not by search path, so they keep working; the app's own use of them is via plain `ILIKE` through PostgREST, which is a `pg_catalog` operator, not part of `pg_trgm`, so no caller needs schema `USAGE` for existing functionality (granted anyway, defensively).
- **`pg_graphql_anon_table_exposed`/`pg_graphql_authenticated_table_exposed`** on `public.answer_feedback` — `REVOKE SELECT ... FROM anon, authenticated`. `scripts/migrate_answer_feedback.sql`'s own comment already documents the intent ("only the backend's secret-key client... can read"); the table-level `SELECT` grant those roles get by default in a new Supabase project was never actually revoked to match, even though RLS itself already blocks every row read for both roles (no `SELECT` policy exists) — this closes the grant to match the documented intent exactly, and removes the table from GraphQL/PostgREST schema discovery.

### 30.4 `public.rls_auto_enable()` — resolved: a benign DDL governance guardrail, now committed

**Severity:** Low (downgraded from the original High once read — see below). Was a genuine unknown in a live production database; is not anymore.

**Where:** the advisor flagged `public.rls_auto_enable()` as callable via `/rest/v1/rpc/rls_auto_enable` by both `anon` and `authenticated`, and as `SECURITY DEFINER`. The operator ran `SELECT pg_get_functiondef(...)` against the live project (2026-08-21) and its body shows it is an **event trigger function** (`RETURNS event_trigger`) that fires on `CREATE TABLE`/`CREATE TABLE AS`/`SELECT INTO` in the `public` schema and runs `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` on the new table automatically — a safety net against a table shipping with RLS off by accident, not something meant to be called directly. A function declared `RETURNS event_trigger` cannot be invoked through a normal call (including a PostgREST RPC call) — only the event-trigger dispatcher can run it — so the advisor's "executable by anon/authenticated" warning is very unlikely to be a live risk in practice; it reflects a privilege-bit check, not an actual reachable call path.

**Deliverable — done:** the function's real definition is now committed to `scripts/sql/rls_auto_enable_setup.sql`, which also `REVOKE`s `EXECUTE` from `PUBLIC`/`anon`/`authenticated` as correct hygiene (there's no legitimate reason those roles need it, even if the call would fail anyway). Run that file. One thing it does **not** resolve: whether a matching `CREATE EVENT TRIGGER ... EXECUTE FUNCTION rls_auto_enable()` actually exists to wire this function to real DDL events, or whether it's dead code with nothing pointing at it. The file's own header has the one-line query to check (`SELECT ... FROM pg_event_trigger WHERE evtfoid = 'public.rls_auto_enable()'::regprocedure`) — run it next time this file is touched and fold the answer into its history.

**Exit criteria:** met for the provenance/grant questions (function committed, execute revoked). Open only on whether the event trigger registration itself exists and is tracked — low priority, since the function is confirmed non-exploitable via direct call either way.

### 30.5 New evidence for the already-open Third-Party-Auth / RLS question (Prompt 34, §21) — not a new decision

**Severity:** Informational — this does not open a new question, it adds one more concrete data point to a question this project has already documented exhaustively (`DECISIONS.md` lines 196-204 and 719, `docs/DATABASE.md`, `docs/LAUNCH_CHECKLIST.md`, `docs/SECURITY.md` §2, and this document's own execution-order row "1b"): whether Supabase Third-Party Auth for Clerk is actually enabled, which determines whether `auth.uid()` resolves to anything for a real signed-in user.

**Where:** `scripts/sql/rag_identity_cache_setup.sql` defines `user_memories_block_client_select`/`user_memories_block_client_write` policies (`USING (false)` for `anon, authenticated`) — an older, fully-locked-down design ("server uses service role only"). `scripts/sql/SUPABASE_RLS_POLICIES.sql` — the file `docs/DATABASE.md` and `docs/SECURITY.md` both describe as the actually-governing policy set — separately defines `user_memories_select_own`/`insert_own`/`update_own`/`delete_own` (`auth.uid()::text = user_id`) on the *same table*, under different policy names. Both are `PERMISSIVE` policies (Postgres's default), which OR together per command rather than overriding each other — so if both scripts have actually been run against the live project, the later, narrower per-owner policies are the ones that actually grant access; the older `_block_client_*` policies become redundant, misleading dead weight rather than an active second gate. If only `rag_identity_cache_setup.sql` was ever run (not `SUPABASE_RLS_POLICIES.sql`), `user_memories` is currently fully locked regardless of the Third-Party-Auth answer, contradicting what `docs/DATABASE.md`/`docs/SECURITY.md` describe as current behavior.

**Deliverable:** no new work — this is additional motivation for Prompt 34's existing "1b" operator action (confirm Third-Party Auth for Clerk in the Supabase dashboard) to happen soon, not a parallel task. Once that's answered and Prompt 34's live RLS check runs, also drop `rag_identity_cache_setup.sql`'s now-redundant `user_memories_block_client_select`/`_write` policies as part of that same pass, so only one tracked file describes this table's access model.

**Exit criteria:** covered by Prompt 34/§21's own existing exit criteria — no separate tracking needed here.

### 30.6 `answer_feedback`'s `WITH CHECK (true)` INSERT policy is intentional, not a finding

**Severity:** None — confirming, not flagging. The advisor's `rls_policy_always_true` warning on `anyone_can_submit_feedback` is expected: this table is deliberately open to anonymous submissions (same pattern as `accept_legal()` in `backend/routes_user.py`), and the migration's own comment already documents that it has no matching `SELECT` policy on purpose. No action.

### 30.7 Ordering

30.1–30.3 and 30.4 (`scripts/sql/rls_auto_enable_setup.sql`) are both ready to run now, independent of everything else. 30.5 has no independent action — fold it into whenever Prompt 34's "1b" is finally answered. 30.6 requires nothing.

**Update (2026-08-21):** 30.4 is resolved (see above). Separately, the first run of §29.6/§29.7's `scripts/migrate_user_preferences_user_id_to_text.sql` failed live with `0A000: cannot alter type of a column used in a policy definition`, naming a policy — "Users can manage their own preferences" — that exists on the real project but in **no tracked SQL file in this repo**, not even `scripts/sql/SUPABASE_RLS_POLICIES.sql`. This is the same "schema drift" pattern as 30.4 and 30.5: a third, previously-unknown live artifact on `user_preferences` alongside the two already-known policy sets. The migration file has been rewritten to discover and carry through whatever policies actually exist on the table at run time (drop, alter, recreate verbatim from what Postgres itself reports) rather than guessing at that policy's clause — safe regardless of what else is live and untracked on this table. Re-run it.

**Update 2 (2026-08-21):** the operator's Supabase SQL Editor saved-snippet list surfaced three more untracked live artifacts, reconciled as follows:
- `scripts/migrate_answer_feedback.sql`'s `CREATE POLICY "anyone_can_submit_feedback"` had no `DROP POLICY IF EXISTS` guard — unlike `CREATE TABLE`/`CREATE INDEX`, Postgres's `CREATE POLICY` has no `IF NOT EXISTS` form, so re-running the file after its first successful run failed with `42710 policy already exists` on that one statement (table/index/RLS-enable above it are all already idempotent and had in fact already succeeded live). Fixed by adding the guard.
- A genuinely new, previously-untracked migration — `ALTER TABLE public.user_preferences ALTER COLUMN prefs SET DATA TYPE jsonb USING prefs::jsonb` — turned out to be the fix for a real type/code mismatch: `scripts/sql/bookmarks_and_preferences_setup.sql` defines `prefs` as plain `text`, but `backend/routes_user.py`'s GET/PUT handlers have always treated it as a JSON object (`isinstance(stored, dict)`), meaning every preferences read likely silently returned empty regardless of what was saved. Committed to `scripts/migrate_user_preferences_prefs_to_jsonb.sql` with its stale `app.py:3089` comment corrected to the real current location and a caveat about the `::jsonb` cast failing on any legacy non-JSON text row.
- The remaining saved snippet the operator described as the "RLS baseline" is byte-for-byte identical to the already-tracked `scripts/sql/SUPABASE_RLS_POLICIES.sql` — confirmed, no new file needed.

---

## 31. Findings from implementing Prompt 29a (§16 Phase 9a rate-limiter unification) (found 2026-08-22)

Prompt 29a's remaining scope (§16.8.2: the Upstash store + single ASGI middleware, D3) shipped — see §16.9 for the full reconciliation. Three independent findings surfaced during that implementation pass, none blocking the others.

### 31.1 The fail-open/fail-closed asymmetric posture has zero test coverage — the single highest-priority gap here

**Severity:** High. This is exactly the property the original Prompt 29a spec called for ("Add a test for each posture with the store stubbed unreachable") and it was never written.

`backend/rate_limit.py`'s `_check()` (lines 207–227) is the function that implements §16.3-L2's deliberately asymmetric posture: the `llm` class fails **closed** when the store raises `_StoreUnavailable` (an unmetered `/ask` during an outage is a budget hole), every other class fails **open** (a reader shouldn't be blocked because Redis blipped). The coverage report for the full pytest run (`pytest -q`, 2026-08-22) shows lines 215–227 — the entire `except _StoreUnavailable` branch, i.e. all of this logic — as **uncovered**. No test currently stubs the store to raise and asserts either posture. If a future edit inverted `fail_open`/`fail_closed` for a class, or broke the `except` branch outright, the full suite would still pass green.

**Fix:** add these two tests to `tests/test_rate_limit.py` (or extend `tests/test_ask.py`) — corrected 2026-08-25, per Prompt 49/§37: no `tests/test_rate_limit_middleware.py` was ever created under that or any other name; `backend/rate_limit.py` was committed for the first time in the same pass that finally landed `tests/test_rate_limit.py` (store abstraction, `get_shared_store()`) and `tests/test_rate_limit_config.py` (`RATELIMIT_ENABLED` boot-time check), and **neither covers `RateLimitMiddleware`'s fail-open/fail-closed dispatch** — this gap is still fully open, not narrowed by that pass: (a) monkeypatch `backend.rate_limit._store` to an object whose `incr()` raises `_StoreUnavailable`, hit `/ask` via `fastapi_client`, assert it still 200s-through-to-fallback-or-succeeds — i.e. is NOT blocked by the limiter itself (fail-closed for `llm` means the request is rejected, not allowed — reread §16.3-L2's exact wording and `_check()`'s `return policy.fail_open, ...` before writing the assertion; the `llm` class's `fail_open=False` means the store-unavailable path returns `allowed=False`, i.e. `/ask` gets a 429-shaped rejection during an outage, which is the intended "closed" behavior). (b) same stubbed-unavailable store, hit a `cheap`/`fanout`/`feedback`-class route, assert it succeeds (fail-open). Also assert `_capture_backend_error` is called in both cases (already wired via `asyncio.to_thread`, just needs a spy). This is the test the original spec asked for and is small — a monkeypatched store object, no real Redis needed.

### 31.2 `RATE_LIMIT_REDIS_URL` is not yet confirmed set in production — an operator action, not a code gap

**Severity:** Medium, time-sensitive. D3 (plan.md §16.1) is closed in code but remains open in effect until this is confirmed.

`backend/rate_limit.py` falls back to the in-process `_InMemoryStore` whenever `RATE_LIMIT_REDIS_URL` is unset, logging a loud startup warning naming the gap. This implementation pass did not have access to Vercel's production environment-variable configuration to confirm whether that variable is actually set. Until it is, production `/ask` traffic is limited per-instance, not globally — the exact D3 defect, just now living at the infrastructure-config layer instead of the code layer.

**Fix:** operator action, matching this project's established pattern for infra-only follow-ups (e.g. Prompt 34's "1b" row): provision an Upstash Redis database (free tier is ample at Sh'elah's volume per §16.3-L2) and set `RATE_LIMIT_REDIS_URL` in the Vercel project's environment variables. Confirm post-deploy by checking `/api/stack/health`'s `security.limiter_store` field reads `"redis"`, not the in-memory fallback string.

### 31.3 `DECISIONS.md` still describes the pre-unification two-limiter state

**Severity:** Low, documentation-only. `DECISIONS.md` (an ADR-style point-in-time record, last touched before this pass) has several passages describing "two independent rate limiters" as current fact — notably the "What was chosen" / "Where it lives" / "Can a user hammer /ask today" block around its rate-limiting decision entry, plus scattered mentions elsewhere (its Clerk-JWKS-outage aside, its production-readiness checklist item 8/9, its cost-model section). None of this was updated by this pass — `DECISIONS.md` was out of this prompt's explicit scope (`plan.md` and `claude_code_prompts.md` only), and it reads as a historical snapshot rather than a living doc, so it's plausible this is intentional. Confirm which convention applies before touching it: if `DECISIONS.md` entries are meant to stay frozen at time-of-writing (a true ADR), skip this; if it's meant to track current state, update the rate-limiting entry to describe the unified middleware and cross-reference `plan.md` §16.9.

### 31.4 Ordering

31.1 is the one with real risk (an untested security-critical branch) and should land first. 31.2 is time-sensitive but not code — flag to the operator independent of 31.1/31.3. 31.3 is optional pending the convention question above.

---

## 32. Findings from implementing Prompts 26/27 (§13 Phase 7 Sonar debt, §14 Phase 8a Vercel quick wins) (found 2026-08-22)

Six independent findings, none blocking the others. §13's status note and §14.8 (above) record what shipped; this section is the follow-up envelope for what that pass found but was out of scope to fix inline.

### 32.1 48 complexity hotspots remain in the §13.1 target files — a curated backlog, not a re-run of the bulk-pass ban

**Severity:** Medium, no urgency — these are maintainability debt, not bugs. `radon cc` (a cyclomatic-complexity approximation for SonarCloud's `python:S3776` cognitive-complexity metric — not equivalent, since Sonar weights nesting more heavily; no CI/SonarCloud credentials were available locally to run the real scan) was re-run after this pass's 3 refactors landed, scoped to the same 5 files §13.1 named plus the two §4-created extraction modules:

```
app.py: _run_ask_question_ai_synthesis (D), ask_question (C), _flatten_primary_sources_for_claude (C),
        _collect_ask_question_context (C), _build_trusted_custom_sources (C), _lookup_lat_lon_from_ip (C)
backend/sefaria_library.py: get_linked_texts (C), _classify_v3_version_texts (C), _parse_v3_response (C),
        search_library (C), get_text (C), _process_ref_candidate_title (C), _flatten_index_titles (C),
        _lookup_canonical_index_title (C), get_category_contents (C), _walk_liturgy_books (C),
        _resolve_index_schema_with_fallbacks (C), _score_catalog_row (C), _search_index_catalog (C),
        _resolve_search_result_categories (C), _prune_and_fix_library_index (C), _resolve_ref_candidates (C)
backend/helpers.py: _looks_like_transliteration (C), _compact_ai_source_entry (C), _lookup_english_word_meaning (C),
        _lookup_hebrew_word_meaning (C), _best_definition_from_lexicon_entries (C), _fill_missing_english_lines (C),
        _canonicalize_community_name (C), _lookup_sefaria_lexicon (C)
backend/routes_library.py: _synthesize_section_refs (C), get_word_meaning (C), _extract_chapters_alt_sections (C),
        _collapse_talmud_leaf_refs (C), export_chapter (C), _extract_topic_alt_sections (C)
asgi.py: ask_async (D), _run_ask_async_ai_synthesis (C), _flatten_sources_for_ai (C)
backend/utils/search_provider.py: _collect_external_global_sources (D), _collect_global_sefaria_sources (C),
        _translate_text_mymemory (C), _translate_text_google (C), _is_sefaria_hit_relevant (C),
        _looks_like_trusted_web_match (C)
backend/utils/text_engine.py: _collapse_markdown_spacing (C), _normalize_ai_answer (C), format_source_citation (C)
```

48 functions total: 3 D-grade (`app.py::_run_ask_question_ai_synthesis`, `asgi.py::ask_async`, `backend/utils/search_provider.py::_collect_external_global_sources`), 45 C-grade. **Fix:** continue exactly the pattern this pass established — pick off the worst remaining offender, refactor it alone (guard clauses → flattened conditionals → early returns), test-anchor, commit, repeat. Start with the 3 D-grade functions; `app.py::_run_ask_question_ai_synthesis` and `asgi.py::ask_async` look, from their names, like plausible near-duplicates of each other (Flask vs ASGI entry points for the same synthesis step) — worth checking whether one delegates to the other before refactoring both independently, since a shared-helper extraction might resolve both at once. Do **not** batch these into one pass — same rationale §13.1 already gives.

### 32.2 The `templates/index.html` dialog double-handling fix is applied and verified but not committed — it is entangled with ~1,900 lines of unrelated pre-existing uncommitted changes

**Severity:** Medium — the fix itself is correct and low-risk, but it currently exists only in the working tree and will be lost on a `git checkout`/`stash`/machine change.

§13.2's "audit for double-handling" requirement was completed: the global `keydown` Escape handler (`templates/index.html`, inside the `document.addEventListener('keydown', ...)` block) had a redundant `closeChapterGrid()` call sitting alongside `chapterGridModal`'s own native `cancel`-event listener — since native `<dialog>` already fires `cancel` on Escape and that listener already calls `closeChapterGrid()`, the duplicate call would double-fire the close logic on every Escape press. It was removed and replaced with an explanatory comment; verified in-browser via the Flask dev server (`.claude/launch.json`'s `flask-dev` config): open trigger, close button, and backdrop click all confirmed working for both `calendarModal` and `chapterGridModal`.

**However**, `git diff --stat templates/index.html` shows 1,402 insertions / 518 deletions against `HEAD` — the fix is a ~10-line change sitting inside a much larger, pre-existing uncommitted diff (present in the working tree before this session started) that includes at minimum: an analytics-consent-gate feature (`initAnalyticsConsent`/`loadVercelAnalytics`, gated on a `Sh'elahAnalyticsConsent` localStorage key per §12.5.3/§8.A.4), two `application/ld+json` JSON-LD blocks (WebSite + Organization schema, per §12.5.5), a `{% from "components/icons.html" import phosphor, phosphor_path %}` template import, and the `#legalModal` → static-footer removal already noted in §13.2's 2026-08-16 update. None of that was authored or reviewed in this session, so committing the whole file risked shipping unverified content under a commit message describing only the dialog fix — the exact mistake this session already made once and fixed (see the git-history note: commit `5b518ce` was `git reset --soft`'d after accidentally bundling 10 unrelated pre-staged files). **Fix:** a dedicated pass needs to review the full pending `templates/index.html` diff (ideally split into separate reviewed commits per feature: analytics consent, JSON-LD, the dialog fix, whatever else is in there), not just apply the one hunk this session touched. Both `calendarModal` and `chapterGridModal` are confirmed already native `<dialog>` elements using `.showModal()`/`.close()` in the current working tree (not yet committed) — §13.2 item 1 (the native-`<dialog>` conversion itself) is functionally done, just uncommitted along with everything else in that file.

### 32.3 §13.1's prediction that `search_provider.py` would "naturally drop below threshold" via §4 extraction did not fully hold

**Severity:** Low, informational — a documentation-accuracy note, not a bug. §13.1 predicted the majority of complexity hits would disappear as a side effect of the `text_engine`/`search_provider` extraction (§4 Phases 1–4). That held for `app.py` broadly, but `backend/utils/search_provider.py` — itself one of the two extraction targets §13.1 named as the destination for moved complexity — still had an E-grade function (`_build_last_resort_web_sources`, radon E/37) after the extraction landed, and per §32.1 still has 6 C-or-worse functions including a new D-grade one (`_collect_external_global_sources`). Extraction moves complexity into fewer, better-named functions; it does not automatically reduce complexity within the destination itself. No action needed — already fixed the specific worst offender this pass — but future extraction-based complexity claims in this doc should be verified with a re-scan of the destination module, not assumed.

### 32.4 `calendarModal` has no backdrop-click-to-close handler; `chapterGridModal` does — an inconsistency, not fixed

**Severity:** Low, UX polish. `chapterGridModal` has a click listener on the dialog element itself that closes on a backdrop click (`chapterGridModal.addEventListener('click', ...)` calling `closeChapterGrid()` when the click target is the dialog backdrop, not its content). `calendarModal` has no equivalent — only its own `cancel` listener (Escape) and an explicit close button/link. This may be intentional (calendar interactions might benefit from a harder-to-accidentally-dismiss modal), or it may be an oversight from whichever earlier pass added `chapterGridModal`'s handler without carrying it to `calendarModal`. **Fix:** confirm intent with the repo owner; if unintentional, add the same backdrop-click pattern to `calendarModal` for consistency — small, low-risk, but a new-behavior change so it needs its own commit and in-browser verification, not a silent addition.

### 32.5 Testing-methodology caveat: this session's browser-automation tool cannot verify native `<dialog>` Escape-to-close end-to-end

**Severity:** Informational — affects how future sessions verify §13.2-style interaction requirements, not application correctness. While in-browser-testing both modals this pass, synthetic `Escape` key presses dispatched via the Claude_Browser automation tool's `computer` action (`key` action, both `"Escape"` and `"Esc"` name variants tried) did **not** trigger the native `<dialog>` close-watcher in the automated browser — the dialog stayed open, `.open` remained `true`. This was initially suspected to be an app bug. Isolated with a minimal bare-`<dialog>` test page containing zero app code: identical non-closing behavior occurred, ruling out an app-specific cause. The underlying `cancel`-event wiring was independently confirmed correct by manually dispatching a `cancel` `Event` via JS (`dlg.dispatchEvent(new Event('cancel', {cancelable:true}))`), which closed the dialog as expected. **Conclusion:** this is a limitation of the automation tool's synthetic key-event dispatch (it does not trigger Chromium's native close-watcher mechanism for `<dialog>`), not an application defect. **Fix:** none needed in application code; noting this so a future session doesn't re-diagnose the same tooling artifact as a regression. True end-to-end OS-level Escape verification for `<dialog>` elements needs either a real (non-synthetic) keypress, a dedicated browser-automation framework with lower-level input dispatch (e.g. Playwright's `page.keyboard.press` — untested here, may or may not have the same limitation), or manual human testing.

### 32.6 `app.py`'s module-level `supabase` import was identified but deliberately not deferred this pass

**Severity:** Low, a real remaining §14.4.1 gap but explicitly scoped out. `app.py` has a module-level `from supabase import create_client` alongside the `anthropic`/`google-genai` imports that *were* deferred in `backend/claude.py` this pass (commit `de819c1`). It was left eager because its blast radius is meaningfully larger: `backend/claude.py`'s SDK imports had exactly 3 call sites needing a lazy-loader call (`_get_client`, `_get_async_client`, `_configure_gemini_client`), all traced and verified; `supabase` is referenced across many route/service files importing from `app.py`, and deferring it safely would need auditing every call site to confirm none run at import time (e.g. as a decorator argument, a route-registration default, or other import-time-evaluated context) before it's safe to defer. **Fix:** a dedicated pass should trace every `supabase`-client usage reachable from `app.py`'s module scope, confirm none execute at import time, then apply the same `_ensure_supabase_loaded()` lazy-singleton pattern used in `backend/claude.py`.

### 32.7 Ordering

32.2 (commit the dialog fix, ideally alongside a full review of the rest of that file's pending diff) is the most time-sensitive — it's real, verified work currently sitting only in a local working tree. 32.1 and 32.6 are both real, no-urgency backlog — pick off 32.1's 3 D-grade functions first. 32.3 is documentation-only. 32.4 needs an operator decision before any code changes. 32.5 needs no code action, just awareness.

---

## 33. Findings from implementing Prompts 28/29b (§14 Phase 8b cache tiers, §16 Phase 9b WAF + cost breaker) (found 2026-08-22)

Eight independent findings, none blocking the others except where noted. §14.10 and §16.10 (above) record what shipped; this section is the follow-up envelope for what that pass found but needed a decision, an operator action, or was out of scope to fix inline. Two items (33.3, and the plan.md corrections it made) were fixed directly in this same pass rather than deferred, following this file's own precedent (e.g. Prompt 19's finding (a)) for cheap, unambiguous corrections — recorded here anyway for the audit trail.

### 33.1 The global cost breaker deviates from §16.3-L3's literal reuse instruction — needs explicit sign-off

**Severity:** Medium — a deliberate design decision, not a bug, but it contradicts what the plan document says to do and should not stand uncorrected without agreement.

§16.3-L3's text says: *"Add a global daily USD ceiling, checked before the provider call, reusing the existing circuit-breaker primitive from `backend/utils/search_provider.py` (§3 reuse rule — do not invent a second breaker)."* `backend/utils/search_provider.py`'s `APIHealth` breaker (also used by `backend/health_check.py`) is an **in-process, per-instance** primitive — exactly the shape §16.1's own D3 defect describes ("per-process counters... no shared store"). Reusing it for the *global* ceiling would mean each of Vercel Fluid's concurrent instances tracks its own independent trip state, so the "global" breaker would in practice only cap spend per-instance, silently multiplied by however many instances are warm — the opposite of what a global ceiling is for.

**What was built instead:** `backend/cost_meter.py::is_global_cost_breaker_tripped()` reuses `backend/rate_limit.py`'s Redis-backed shared store (new `get`/`setex` methods + a `get_shared_store()` accessor) — the same cross-instance-safe store Phase 9a's D3 fix built for rate limiting. This satisfies the §3 reuse rule's *intent* (one shared primitive, not a second bespoke store) while deviating from its *literal text* (a different existing primitive than the one named).

**Fix:** this is a judgment call the plan's author should explicitly bless or reject, not something to leave silently substituted. If blessed, §16.3-L3's text should be corrected in place to name `backend/rate_limit.py`'s store instead of `search_provider.py`'s breaker, so a future reader doesn't "fix" this back to the D3-recreating version.

**✅ Resolved 2026-08-23 (Prompt 45).** Blessed as-is — the reasoning above is sound (the literal instruction would recreate the exact per-instance defect D3 fixed) and there was no competing argument found for reverting to `search_provider.py`'s breaker. §16.3-L3's text corrected in place (see that section, updated 2026-08-23) to name `backend/rate_limit.py`'s shared store as the authoritative primitive for any future global/cross-instance state.

### 33.2 `X-Deploy-Hash`'s cache-purge-on-deploy assumption is not independently verified

**Severity:** Low — no correctness risk given the short TTLs involved (§14.3's tiers cap at 24h), but the header is currently trusted for more than it's been proven to do.

`apply_response_cache_policy()` adds `X-Deploy-Hash` (sourced from the existing `SENTRY_RELEASE`/`VERCEL_GIT_COMMIT_SHA` constant) to every non-private cacheable response, on the commonly-documented assumption that Vercel's CDN auto-purges cached entries on deploy. This was not checked against current Vercel docs in this pass. **Fix:** confirm the assumption (Vercel's edge-cache-invalidation-on-deploy behavior, current as of whenever this is read — it is exactly the kind of platform detail that drifts) before relying on this header for anything beyond diagnostics; if false, a corpus edit could theoretically serve a stale cached response until its `s-maxage` naturally expires.

**✅ Verified 2026-08-23 (Prompt 45), and the underlying claim needs a correction.** Fetched Vercel's current docs (`/docs/caching/cdn-cache`, `/docs/caching/cdn-cache/purge`, both `last_updated` 2026-04/06-2026). The assumption in this section's wording — "Vercel's CDN auto-purges cached entries on deploy" — is not literally what happens, but the practical outcome it's used for (a fresh deploy won't keep serving stale cached content) **is** true, via a different mechanism: per the purge doc, the cache key is derived from method + URL + host + **the unique deployment URL** + scheme, and "since each deployment has a different cache key, you can promote a new deployment to production without affecting the cache of the previous deployment." There is no active purge event on deploy — the old deployment's cache entries simply become orphaned (unreachable once production traffic points at the new deployment's distinct cache key) and expire on their own TTL, unserved. **Correction: `X-Deploy-Hash` itself has no functional role in this mechanism** — Vercel doesn't use custom response headers as part of its cache key (only `Vary`-listed request headers do that), so the header does not "invalidate" anything. It remains useful only as a diagnostic (matching a served response to the deploy that produced it), which is how it's actually used in this codebase — no code or header behavior needs to change, only this section's framing. `docs/VERCEL_COST_OPTIMIZATION.md`'s `X-Deploy-Hash` caveat updated to match.

### 33.3 plan.md §14.4.4/§24.2/§24.5 were stale, claiming AI-provider circuit breakers were unwired — corrected in this same pass

**Severity:** Informational — already fixed, recorded for provenance. `docs/VERCEL_COST_OPTIMIZATION.md`'s first draft (written during this pass, before this cross-check) repeated the same stale claim in its §8 "explicitly not touched" section and has also been corrected.

All three plan.md sections asserted, accurately as of when each was written (2026-08-19), that `backend/claude.py` never consulted `health.is_healthy()` before dispatching to Gemini/Claude. **Prompt 39/§26.1 shipped this fix the very next day (2026-08-20)** — confirmed by grep: `backend/claude.py:27` imports `health`, and `is_healthy`/`record_success`/`record_failure` are called at all three `/ask` provider entry points. This pass found the staleness while writing §33.1's neighboring circuit-breaker context, and corrected all three plan.md sections plus the doc in place rather than filing it as a new finding, since it required no judgment call — just re-reading the current code. **No further action needed.**

**Reconfirmed accurate 2026-08-23 (Prompt 45), no drift since.** `grep` still shows `health.is_healthy`/`record_success`/`record_failure` wired at all three provider entry points in `backend/claude.py`.

### 33.4 §14.7.1/§14.7.4's baseline capture and preview-deploy verification remain pending — operator/deploy-only, cannot be completed from this headless session

**Severity:** Medium — these are the exit criteria that actually prove the cache-tier work is safe and effective in production; everything else is a proxy for them.

- **§14.7.1 baseline capture:** 30 days of Vercel Usage-dashboard figures per meter, plus the cached-vs-uncached request ratio, need recording *before* this change's impact is measurable. Recommend capturing now if not already, then re-measuring at 7 and 30 days post-deploy.
- **§14.7.4 preview-deploy verification:** confirm cacheable routes return expected headers and show CDN `HIT` on a second request; confirm `/ask` and every authed route still show `no-store`; confirm static assets are edge-served (§14.3.2, also unverified); confirm no cross-user leakage on any newly cached route (fetch as user A, then as user B, compare).

**Fix:** both need a live Vercel deployment and dashboard access this session did not have — same category as prior deploy-only steps (Prompt 11/§18, Prompt 24/§11). Not something to script around; schedule as the next deploy's verification checklist.

**⏸ Still pending 2026-08-23 (Prompt 45).** No Vercel dashboard access from this headless session either — unchanged from 2026-08-22. Related, and checked this pass: `DAILY_BUDGET_USD` is still empty in the local `.env.example` (line 111); this session cannot confirm or deny whether a real value is set in the live Vercel project's env vars (that requires the same dashboard access this finding is already blocked on). If unset in production, the global cost breaker (§16.3-L3) is live code with an inert threshold — flagging loudly per the operator-confirmation discipline used elsewhere in this document (e.g. Prompt 43/§31 STEP 2's `RATE_LIMIT_REDIS_URL` check). Operator: confirm `DAILY_BUDGET_USD` is set in the live Vercel project alongside this checklist.

### 33.5 Prompt 29b shipping newly unblocks Prompt 33c's (§20c) multi-account-budget-bypass decision

**Severity:** Medium — a decision this project has explicitly said must not be left unresolved, and it just became answerable.

§20c / Prompt 33c recommends option A (accept the per-user ceiling as a good-faith guardrail, not an anti-abuse control) "conditional on Prompt 29b shipping," since A's honesty depends on a real global breaker existing as the actual ceiling. Prompt 29b has now shipped (§16.10). **Fix:** make the §20c decision now — update `plan.md` §20.2 and Prompt 33c's row to record option A as chosen (or a different choice, if the operator disagrees), rather than leaving it open now that its stated precondition is satisfied.

**✅ Resolved 2026-08-23 (Prompt 45).** Option A chosen — recorded with rationale in `plan.md` §20.2 PHASE 20c, `.env.example:50-61`, and `docs/SECURITY.md` §5. One caveat surfaced while closing this out and not previously connected to this decision: `DAILY_BUDGET_USD` (the global breaker's own threshold) is still empty as of this date (§33's STEP 6 / §24.4), so Option A's honesty currently rests on the WAF (§33.8, still not entered in the dashboard) rather than the breaker, which is inert until an operator sets that value. This isn't a reason to re-open the decision — B and C are worse on their own merits regardless — but it does mean the "the global breaker is the real ceiling" half of A's justification is not yet true in production, only in code.

### 33.6 The frontend breaker-paused banner shipped without a Retry-After countdown — the original spec assumed a response shape the implementation doesn't have

**Severity:** Low — a scope note, not a defect. The banner itself (§16.10) is complete, tested live in both themes, and satisfies the WCAG/motion/color-only requirements in `.agents/ENGINEERING_RULES.md`.

The original Prompt 29b Part C text modeled a "Retry-After countdown" on `formatCountdownDuration`/`startCountdown`, which key off a `Retry-After` HTTP header — a `429`-response pattern. The actual breaker-paused response is a `200 OK` with `meta.breaker_tripped: true` (chosen so the existing `/ask` success-path rendering — source box, feedback widget, everything — works unchanged with zero frontend logic beyond the banner itself). There is no `Retry-After` header to key a countdown off, and no other real "when will this clear" signal exists server-side (the breaker resets on UTC midnight by construction — `_fetch_today_usage_rows()`'s day boundary — but that boundary is not currently surfaced in the response). Shipped a static "try again after midnight UTC" string instead. **Fix:** decide whether a real countdown is still wanted; if so, it needs a new response field (e.g. `meta.breaker_resets_at`, an ISO timestamp) rather than force-fitting the existing `Retry-After`-based countdown helpers onto a response shape that has no such header.

**⏸ Decided-to-defer 2026-08-23 (Prompt 45), not built.** The breaker has never yet tripped in production (it's inert until `DAILY_BUDGET_USD` is set — §33's STEP 6), so there is no real-usage signal yet that the static string is actually a problem for readers. Building `meta.breaker_resets_at` now means adding a new response field threaded through `backend/cost_meter.py` → `asgi.py` → the frontend's countdown helpers — real production-facing surface area — on spec, for a UX gap that has not been observed to matter and is explicitly this section's own "Low severity... a scope note, not a defect." Left as the static string; revisit once the breaker is live with a real threshold and either the paused-state banner is actually seen by users or the repo owner wants it built proactively. Not closed as "rejected" — closed as "not worth building blind."

### 33.7 A real circular import was found and fixed during this pass — corrects an earlier (pre-this-session) claim that none existed

**Severity:** Informational — already fixed, regression-covered, recorded for provenance.

An earlier pass's plan for `is_global_cost_breaker_tripped()` asserted `from backend.rate_limit import get_shared_store` as a top-level import in `backend/cost_meter.py` carried "no circular-import risk." That claim was wrong — it didn't trace `backend/helpers.py`'s transitive imports. Empirically confirmed via `python3 -c "import backend.cost_meter"`: `ImportError: cannot import name 'record_llm_call' from partially initialized module 'backend.cost_meter'`, tracing the cycle `cost_meter → rate_limit → helpers → search_provider → claude → cost_meter`. Fixed with a function-local (deferred) import instead, matching this file's own pre-existing lazy-import convention elsewhere. **No further action needed** — flagged here only as a reminder that import-cycle claims in this codebase need `python3 -c "import ..."` verification, not just a mental trace, before being trusted.

**Reconfirmed accurate 2026-08-23 (Prompt 45):** `python3 -c "import backend.cost_meter"` still succeeds cleanly.

### 33.8 The WAF L1 layer is specified but not yet applied in the Vercel dashboard

**Severity:** Medium — L2 (rate-limit middleware, Phase 9a) and L3 (cost breaker, this pass) are both live; L1, the layer that makes an attack **free** rather than merely rate-limited, is still not configured.

`docs/SECURITY.md` §7 has the complete dashboard spec per §16.3-L1: the one rate-limit rule (`POST /ask`, IP+JA4 key, 60s fixed window, deploy in Log mode first), and the three custom rules (scanner-path deny list, non-GET/POST/HEAD/OPTIONS method deny, one deliberately-empty incident-response slot). This is a manual dashboard action — the CLI is not authenticated in this environment — and was written, not applied, in this pass. **Fix:** an operator needs to actually enter these rules in the Vercel dashboard, deploy in Log mode, read a week of real traffic, then switch to enforce per the doc's own explicit instruction not to guess the threshold.

**⏸ Still pending 2026-08-23 (Prompt 45).** Unchanged — no CLI/dashboard credentials in this headless session either. `docs/SECURITY.md` §7's spec re-read this pass and still matches the live rate-limit/WAF code shape (§16.9); nothing in it needs revision before an operator applies it.

**✅ Done 2026-08-26 (Akiva, Vercel dashboard).** All 3 rules entered. ⚠️ **One correction to the spec above, found by actually doing it:** the "one rate-limit rule + three custom rules" framing was wrong — Hobby tier has **3 total custom firewall rules, full stop**, and rate-limiting is an action type available on an ordinary custom rule, not a separate quota. There was never a 4th slot to leave empty for incident response. The 3 slots actually in use: the `/ask` rate-limit rule (IP+JA4, 60s fixed window, deployed in Log mode per the doc's instruction), the scanner-path deny list, and the method deny rule — nothing held in reserve. `docs/SECURITY.md` §7 corrected to match. If an incident-response slot is wanted later, it has to come from merging two of the three into one rule or upgrading off Hobby tier.

### 33.9 Ordering

33.4 (baseline/preview-deploy verification) and 33.8 (WAF dashboard entry) are both operator/deploy-only and should be scheduled together against the same deploy window. 33.1 (breaker-primitive deviation) and 33.5 (§20c decision) both need a human decision, not code, and are otherwise independent — 33.5 is the more time-sensitive of the two since this project has flagged it as a must-not-leave-unresolved item twice already. 33.2 is a quick doc-verification task. 33.6 needs a product decision (is the countdown still wanted) before any code follow-up. 33.3 and 33.7 need no further action, kept for the record.

**✅ Prompt 45 pass complete, 2026-08-23.** 33.1 blessed and corrected in place. 33.2 verified against current Vercel docs, with a correction to the mechanism claimed (deployment-scoped cache keys, not an active purge — `X-Deploy-Hash` itself is diagnostic-only). 33.3 and 33.7 reconfirmed accurate, no drift. 33.5 decided (Option A). 33.6 decided-to-defer (not built, rationale recorded). 33.4 and 33.8 remain genuinely blocked on operator/dashboard access this headless session doesn't have — unchanged. One new caveat surfaced connecting 33.4/33.5/33.6/33.8: `DAILY_BUDGET_USD` is still unset, so the global breaker Option A leans on is currently inert in whatever environment doesn't have it configured — see the new follow-up prompt (§34) for what remains genuinely open after this pass.

## 34. Findings from implementing Prompt 45 (§33 follow-up: cost-breaker sign-off, §20c decision, cache-purge verification) (found 2026-08-23)

Two independent findings, neither requiring code by itself. §33 above (this pass) closed six of its eight items outright and left two (§33.4, §33.8) as already-known operator/deploy-only blockers, unchanged in kind — those are not repeated here. This section is only what's newly found.

### 34.1 The uncommitted working tree has grown, not shrunk, since Prompt 41 flagged it — now 181 paths, spanning at least six more "✅ Done" prompts' worth of unrecorded work

**Severity:** High — this is the same finding Prompt 41/§28.1 raised on 2026-08-20 (then ~150 files), left for the repo owner's judgment rather than resolved, and it has been getting worse every pass since rather than better. Two full prompts of real, verified, individually-described work (Prompt 44's hotspot refactors + `templates/index.html` dialog fix, Prompt 45's own doc edits) have now landed on top of the same unrecorded base.

`git status --porcelain` at the start of this pass showed **181** modified/untracked paths (75 untracked, 97 modified, 6 added, 3 deleted) — the last real commit is still `ab98b59` (2026-08-22 19:19), the same one Prompt 41 could see partially forming. One specific, previously-flagged item has visibly grown, not landed: `templates/index.html`'s working-tree diff was "~1,900 lines" per Prompt 44/§32.2 (which named committing it, split by feature, as "the most time-sensitive item" in that prompt); it is now measured at **1,931 lines changed (1,413 insertions, 518 deletions)** against the same last commit — i.e., Prompt 44's STEP 1 was not executed, and more uncommitted change has accumulated on top of the exact file that prompt flagged as most urgent. This pass (Prompt 45) added further uncommitted edits to `plan.md`, `claude_code_prompts.md`, `.env.example`, `docs/SECURITY.md`, and `docs/VERCEL_COST_OPTIMIZATION.md` — consistent with, not separate from, this same problem.

**This is not a new finding in substance — it is the same finding, unresolved, now larger.** Per Prompt 41/§28.1's own established precedent (reaffirmed by this project's operating discipline, e.g. Prompt 33a's "20a.5 backfill — decision stated, not executed... mutating financial ledger rows in an unattended pass is exactly the kind of action that needs a human reviewing output first, not a scheduled task executing it autonomously"), this pass did **not** run a bulk `git add -A && git commit`, for the same reason Prompt 41 declined to: a single bulk commit would stop the growth but forfeit the per-prompt commit granularity this project's own §23 schema-provenance discipline and any future `git bisect`/audit depend on, and around 30 of these 181 paths are `.claude/` skill/session files, `graphify-out/` cache artifacts, and other non-source paths whose inclusion needs a human's judgment about what belongs in version control at all, not a script's.

**Fix — repo owner action needed, now clearly overdue:** the repo owner needs to either (a) confirm this pass may proceed with the incremental, per-prompt commit plan Prompt 41 already proposed (oldest first, one commit per completed Prompt row, using each row's own evidence paragraph as the commit-message basis), or (b) do it themselves, or (c) explicitly accept the risk and say so with a date, so this stops silently recurring in every subsequent findings-prompt. Recommend treating this as the literal first action of whatever prompt runs next, ahead of any new feature work — the backlog does not get easier to reconstruct by waiting.

### 34.2 `docs/SECURITY.md` §5's rate-limiter description is stale — it still describes the pre-unification Flask-Limiter architecture Prompt 43/§31 (2026-08-22) replaced

**Severity:** Low — documentation-only, no runtime risk (the code itself is correct and current), but a reader relying on §5 today gets a materially wrong picture of how rate limiting actually works.

`docs/SECURITY.md` §5's "Rate limiters confirmed active in prod" bullet still describes `Flask-Limiter` as a hard dependency with a blanket `60/min` default plus a separately-described independent in-process check in `asgi.py`, and its very next bullet ("Known, already-documented gap") still frames the in-memory-counter problem as an open, unaddressed D3 defect needing "a shared store... which is an infrastructure decision/new dependency the repo owner hasn't provisioned yet." Both of these were true when §5 was last written but are now stale: Prompt 43/§31 (landed 2026-08-22, see `plan.md` §16.9) replaced Flask-Limiter and asgi.py's independent limiter with one unified `backend/rate_limit.py` Starlette middleware backed by an Upstash Redis shared store (`RATE_LIMIT_REDIS_URL`) — confirmed this pass via `grep`, every live reference to `Flask-Limiter`/`flask_limiter` in the codebase is now a historical code comment ("used to", "prior state"), not an active dependency (`requirements.txt`'s own comment: "Replaces Flask-Limiter, which used to sit on app.py alongside..."). **Fix:** rewrite §5's rate-limiter bullets to describe `backend/rate_limit.py`'s actual current shape (identity-aware keys, the llm-class fail-closed / everything-else fail-open posture Prompt 43/§31.1 added test coverage for, the `RATE_LIMIT_REDIS_URL`-set-or-fallback behavior) instead of the superseded Flask-Limiter description. Not done in this pass — out of Prompt 45's own scope (§33 is about the cache-tier/WAF/cost-breaker pass, not the rate-limiter doc), flagged here rather than fixed inline to avoid scope creep into an unrelated section, matching this project's own discipline about not silently expanding a prompt's boundary.

### 34.3 Ordering

34.1 is the more urgent of the two — it is a repeat, growing finding this project has now surfaced twice (Prompt 41/§28.1, originally found while implementing Prompt 36, and again here) without resolution, and it is a repo-owner decision, not an engineering task. 34.2 is a small, independent, no-urgency documentation fix that can land whenever, including bundled with any other pass that touches `docs/SECURITY.md`.

## 35. Findings from implementing Prompt 29c (§16.6 Phase 9c: identity-aware quotas, Turnstile, mitigation observability) (found 2026-08-24)

One finding, code-only, no product decision needed — but it is the most severe defect any findings section has surfaced to date: a live, unguarded crash on the authenticated `/ask` path.

### 35.1 Every authenticated call to the real, production `/ask` endpoint currently returns HTTP 500 — `_fetch_user_memory_summaries()` unconditionally touches Flask's global `request` from a route that has no Flask request context

**Severity:** Critical — this is not a degraded-mode or edge-case defect. Any signed-in user asking any question via the actual deployed endpoint gets a 500, every time, with no fallback. Prompt 29c's own identity-aware-quota tests were the first tests in this suite ever to simulate an authenticated `/ask` caller end-to-end, and two of the six new tests (plus one Turnstile test) failed against real code on first run because of this — not because of anything wrong in this pass's own rate-limit/Turnstile logic.

**Root cause chain**, confirmed by reading each frame, not just the traceback:

1. `asgi.py`'s native FastAPI `POST /ask` route (`ask_async`, the one actually reachable in production — it is registered before `fastapi_app.mount("/", WSGIMiddleware(flask_app_module.app))`, so it shadows `app.py`'s own Flask `/ask` route, which is now dead code except under a bare Flask `test_client` or `python3 app.py` direct-run dev mode) calls `_collect_ask_async_context()` (`asgi.py:277`).
2. That function unconditionally schedules `_fetch_user_memory_summaries(user_id, ...)` via `asyncio.to_thread` inside an `asyncio.gather` (`asgi.py:296-302`) — unconditionally meaning it runs on every request, authenticated or not; the function itself is what decides whether to do anything.
3. `backend/rag.py::_fetch_user_memory_summaries()` (`backend/rag.py:312-321`) correctly no-ops for anonymous callers (`if not user_id: return []`) but for any truthy `user_id` calls `app._get_user_scoped_supabase_client()` with **no guard at all** — the existing `try/except` in this function wraps only the later `.execute()` call, not client construction.
4. `app.py::_get_user_scoped_supabase_client()` → `_get_request_supabase_client()` → `_extract_supabase_access_token()` → `backend/auth.py::_extract_bearer_token()` (`app.py:1037-1043`, `app.py:1017-1034`, `app.py:984-1014`), and that last function unconditionally reads `request.headers.get("Authorization", "")` off Flask's global `request` proxy — a Werkzeug `LocalProxy` that only resolves inside an active Flask request context.
5. `asgi.py`'s native route runs entirely inside FastAPI/Starlette with **no Flask context pushed at any point** — confirmed by exercising this path via `fastapi_client` (the project's own `httpx.AsyncClient`-over-`asgi.fastapi_app` test fixture) with a monkeypatched authenticated `user_id`. The proxy access raises `werkzeug.local.LocalProxy._get_current_object()`'s `RuntimeError: Working outside of request context.`
6. `asgi.py:756-767`'s inner `try/except Exception as ai_error` does **not** catch this — it only wraps `_run_ask_async_ai_synthesis`, and the crash happens earlier, at the `ctx = await _collect_ask_async_context(...)` call on `asgi.py:739`, outside that inner block. It is instead caught by the outer handler at `asgi.py:771-783`, which calls `_capture_backend_error("ask_route_critical_error_async", ...)` and raises `HTTPException(status_code=500, ...)`. So the failure is silent to the caller beyond a generic 500 — no `RuntimeError` or "auth" wording ever reaches the client, and unless Sentry alerting on `ask_route_critical_error_async` is actively watched, this can go unnoticed indefinitely while every authenticated `/ask` call fails.

**Why this was never caught:** `grep -rn "Authorization" tests/test_ask.py` before this pass showed zero prior tests attached an `Authorization` header to a `/ask` call — every existing `/ask` test exercised the anonymous path only, where `user_id` is `None` and `_fetch_user_memory_summaries()`'s early return means the broken chain below it is never reached. The Clerk-auth wiring in `asgi.py` (`extract_user_id_from_bearer_value`) and the Supabase-client wiring in `app.py`/`backend/rag.py` were each independently correct in isolation; nothing in this codebase's own test suite previously exercised both together on the ASGI-native path.

**Fix:** `_extract_bearer_token()` (and by extension `_extract_supabase_access_token()`, `_get_request_supabase_client()`, `_get_user_scoped_supabase_client()`) needs to stop assuming a Flask request context is always live. The cleanest shape: thread the already-parsed `Authorization` header value down from `asgi.py::ask_async()` — which parses it once already, for `extract_user_id_from_bearer_value()` — as an explicit parameter through `_fetch_user_memory_summaries(user_id, ..., bearer_token=...)` → `_get_user_scoped_supabase_client(bearer_token=...)` → `_get_request_supabase_client(bearer_token=...)` → `_extract_supabase_access_token(bearer_token=...)`, falling back to the current Flask-`request`-reading behavior only when no explicit token is passed (so the Flask-side callers of these same functions, e.g. `routes_user.py`, keep working unchanged). This avoids a second, divergent implementation of the same token-extraction logic (this project's own §2 "live, divergent duplication" anti-pattern) while fixing the actual crash. A minimal guard (wrapping the whole chain in `_fetch_user_memory_summaries()` in a broad `try/except RuntimeError: return []`) would silence the crash but would make user-memory retrieval silently fail for every authenticated user forever, trading a loud, alertable 500 for a quiet feature outage — not recommended as the real fix, though it would be an acceptable **stopgap** to ship immediately (before the parameter-threading fix) precisely because it converts "every authenticated question fails outright" into "every authenticated question succeeds but without memory personalization," which is a strictly smaller regression while the real fix is implemented.

**Not fixed in this pass** — Prompt 29c's own scope is identity-aware quotas/Turnstile/observability, not this unrelated pre-existing defect; per the scheduled task's own standing instruction, this is documented here and handed off as `claude_code_prompts.md` Prompt 47 rather than fixed inline, to keep this pass's diff scoped to what it was asked to build. The three new tests this pass added that would otherwise have tripped over this bug (`tests/test_ask.py::TestAskIdentityAwareQuotas::test_authenticated_and_anonymous_share_ip_but_separate_buckets`, `::test_authenticated_per_minute_allowance_exceeds_anonymous`, `TestAskTurnstileGate::test_authenticated_user_bypasses_turnstile_entirely`) stub `app._get_user_scoped_supabase_client` to `None` with an inline comment pointing back to this section, so they exercise only the rate-limit/Turnstile behavior they are named for and do not mask this finding as a passing test.

**✅ Resolved 2026-08-25 (Prompt 47).** Implemented exactly the recommended fix, not the stopgap: threaded an explicit `bearer_token`/`authorization` parameter down the full chain, each function falling back to Flask's `has_request_context()`-gated `request` global only when no explicit value is passed, so Flask-side callers (`routes_user.py`, etc.) are unchanged —

- `backend/auth.py::_extract_bearer_token(authorization=None)` — uses the passed value when given; when not, only touches Flask's `request` if `has_request_context()`, else returns `None` (previously: unconditional `request.headers.get(...)`, the exact crash site).
- `app.py::_extract_supabase_access_token(bearer_token=None)` → `_get_request_supabase_client(bearer_token=None)` → `_get_user_scoped_supabase_client(bearer_token=None)` — each forwards the parameter down and, in `_extract_supabase_access_token`, short-circuits to `None` before the cookie-reading fallback if `not has_request_context()` (nothing left to try once the explicit bearer_token has already been checked).
- `backend/rag.py::_fetch_user_memory_summaries(user_id, limit=None, bearer_token=None)` — forwards `bearer_token` into `app._get_user_scoped_supabase_client(bearer_token)`.
- `asgi.py::_collect_ask_async_context(..., bearer_token=None)` — passes `bearer_token` into the `_fetch_user_memory_summaries` call scheduled via `asyncio.to_thread`; `ask_async()` (the route's only caller of this function) passes its own already-parsed `authorization` header value as `bearer_token=authorization`.

Per this section's own suggestion, no second/divergent token-extraction implementation was introduced — the existing `extract_user_id_from_bearer_value()` pattern (explicit-value-with-fallback) was reused, not reinvented.

**Tests:** the three workaround monkeypatches this section called out (`TestAskIdentityAwareQuotas::test_authenticated_and_anonymous_share_ip_but_separate_buckets`, `::test_authenticated_per_minute_allowance_exceeds_anonymous`, `TestAskTurnstileGate::test_authenticated_user_bypasses_turnstile_entirely`) were removed. A new `tests/test_ask.py::TestAskAuthenticatedMemoryRetrieval::test_authenticated_call_with_real_memory_row_succeeds_end_to_end` proves an authenticated `/ask` call through `fastapi_client` (the native ASGI route, no Flask context) reaches `_fetch_user_memory_summaries()`, receives a real memory row via a mocked Supabase response, and returns `200` — the exact case that previously 500'd. `tests/test_auth.py` gained two direct unit tests for the new fallback behavior (`test_explicit_authorization_value_needs_no_flask_context`, `test_no_explicit_value_and_no_flask_context_returns_none_not_raise` — the latter is a literal regression test for the `RuntimeError: Working outside of request context.` crash this section documents). `tests/test_rag.py` had four pre-existing `_get_user_scoped_supabase_client` monkeypatch lambdas updated from zero-arg to `lambda bearer_token=None: ...` (the new parameter would otherwise raise `TypeError` against them), plus a new `test_bearer_token_threaded_to_client_factory` proving the parameter actually reaches the client factory rather than being silently dropped.

**Verification:** `RATE_LIMIT_REDIS_URL= .venv/bin/python3 -m pytest --no-cov -p no:warnings` (full suite, not just the touched files — `RATE_LIMIT_REDIS_URL` blanked only for this verification run, see §36 below for why) → **1642 passed, 0 failed**, confirming both the fix and that `TestAskFlask` (the Flask-mounted `/ask` path, dead in production but still tested) stayed green. `graphify update .` run clean (8397 nodes, 10563 edges, 584 communities).

### 35.2 Ordering

Single finding, no ordering needed. This should be treated as the single highest-priority item in whatever prompt runs next — ahead of any further feature work and ahead of §34.1's uncommitted-backlog cleanup — since it is a complete, currently-live functional outage for a whole class of real users (every signed-in caller), not a latent risk or a documentation gap.

**✅ Resolved 2026-08-25 (Prompt 47).** See §35.1.

## 36. Findings from implementing Prompt 47 (§35.1 authenticated `/ask` 500 fix) (found 2026-08-25)

One finding, code-only, test-infrastructure scope — surfaced only because verifying §35.1's fix required running the full suite instead of just the three touched files, which is not this project's habit for every prompt.

### 36.1 `tests/conftest.py` never blanks `RATE_LIMIT_REDIS_URL` — a developer's real `.env` credential leaks into every test run, causing both flaky `RuntimeError: Event loop is closed` failures and live writes to a real, shared Upstash Redis instance during ordinary test runs

**Status: ✅ Fixed and committed 2026-08-26, commit `9a7c159`.** Landed exactly as the "Fix" bullet below specifies — `os.environ.setdefault("RATE_LIMIT_REDIS_URL", "")` added to `tests/conftest.py`'s existing blank-before-import block, comment mirroring the `SENTRY_DSN` pattern. Discovered independently during the §37.2 follow-up work below (a real Upstash URL in this developer's `.env` was causing `tests/test_routes_feedback.py::TestFeedbackRateLimit` to fail against accumulated live counters, not the expected in-process store), then traced back to this exact pre-existing, already-documented gap rather than treated as a new bug. Verified: full suite with a real `RATE_LIMIT_REDIS_URL` present in the shell — `tests/test_routes_feedback.py`'s 9 tests all pass against `_InMemoryStore`, no `RuntimeError: Event loop is closed` anywhere in the run.

**Severity:** Medium — not a production defect (this only affects local test execution on a machine with a real `.env`), but it is a genuine "tests touch a live external resource" issue with two distinct bad outcomes, and it directly caused intermittent, hard-to-explain test failures while verifying §35.1.

**Root cause chain:**

1. `tests/conftest.py:19-33` sets a block of `os.environ.setdefault(...)` calls *before any app-level import*, specifically so that when `app.py`'s own `load_dotenv()` runs later at import time, `load_dotenv()`'s default `override=False` behavior means it will not clobber a var conftest already set. Two of these (`SENTRY_DSN`, `SENTRY_DSN_BROWSER`, `tests/conftest.py:30-33`) are explicitly blanked to `""` with a comment: "a developer's local .env may set these to real DSNs, and load_dotenv() ... won't override an already-set var." This is the correct, already-established pattern for exactly this class of problem.
2. `RATE_LIMIT_REDIS_URL` gets no equivalent treatment anywhere in `tests/conftest.py` — confirmed via `grep -n "RATE_LIMIT_REDIS_URL" tests/conftest.py`, zero matches.
3. `backend/rate_limit.py:231`: `RATE_LIMIT_REDIS_URL = (os.environ.get("RATE_LIMIT_REDIS_URL") or "").strip()` — a module-level read, evaluated once at import time, with no test-awareness of any kind.
4. `backend/rate_limit.py:257`: `_store: _RateLimitStore = _build_store()` — also module-level, also evaluated once at import. `_build_store()` (`:239-248`) returns a real `_RedisStore(RATE_LIMIT_REDIS_URL)` (`:106-118`, wrapping `redis.asyncio.Redis.from_url(...)`) whenever `RATE_LIMIT_REDIS_URL` is truthy, and only falls back to the in-process `_InMemoryStore()` (the module's own docstring calls this "plan.md §16.1 D3, kept solely as the local-dev/test fallback") when the var is empty.
5. On this developer's machine, `.env` (not committed, but loaded by `load_dotenv()`) sets a real, live credential at line 50: `RATE_LIMIT_REDIS_URL="rediss://default:...@frank-osprey-109289.upstash.io:6379"`. Because step 2 never blanks this var first, `load_dotenv()` is free to set it, and every test run on this machine builds a **real** `_RedisStore` bound to an actual Upstash instance — not the intended offline fallback.
6. Consequence A (the symptom that surfaced this): `redis.asyncio.Redis.from_url(...)` lazily creates its connection pool bound to whichever asyncio event loop is running at first use. pytest-asyncio's default fixture scope creates a **new** event loop per async test function. `_store` is a module-level singleton built once at import and reused across every test in the session — so once one test's Redis connection is established under loop A, a later test running under loop B hits `backend/rate_limit.py:216`'s `raise _StoreUnavailable(str(exc)) from exc`, wrapping the underlying `RuntimeError: Event loop is closed`, observed as a wide, seemingly-unrelated wave of failures across `TestAskFastAPI`, `TestAiCitedSourcesSchemaParity`, `TestAskTransportKeySetParity`, and others once enough async `/ask`-hitting tests accumulate in one `pytest` invocation.
7. Consequence B (not yet symptomatic, but real): every `incr()`/`get()`/`setex()` call made by any test that exercises the rate limiter is a genuine network round-trip against a live, shared Upstash instance, incrementing real fixed-window counters keyed by whatever identity/IP the test used. This is silent — nothing in the test output indicates a real external service was touched — and could, in principle, affect that instance's actual rate-limit state if the same keys are ever reused outside tests (unlikely given test IPs, but not guaranteed, and definitely not the intended isolation for an offline test suite).
8. `.github/workflows/ci.yml` does not set `RATE_LIMIT_REDIS_URL` either (confirmed via grep — only `RATELIMIT_ENABLED=false` appears, and per `backend/rate_limit.py`'s own docstring at `:34-43` in `tests/conftest.py`, that flag is now a no-op for this unified limiter). CI is very likely unaffected in practice, since CI runners have no developer `.env` file to leak from — but this has not been independently confirmed, and the gap in `tests/conftest.py` is the same regardless of whether any given environment happens to have the var set.

**Fix (not yet implemented — flagged per the standing scheduled-task instruction to hand off new findings rather than scope-creep them into Prompt 47's own diff):** add `RATE_LIMIT_REDIS_URL` to `tests/conftest.py`'s existing blank-before-import block, mirroring the `SENTRY_DSN` pattern exactly:

```python
# Blank out — a developer's local .env may set this to a real Upstash URL,
# and load_dotenv() (called on app import) won't override an already-set
# var. Forces backend/rate_limit.py's module-level _store singleton to
# build the in-process _InMemoryStore fallback instead of a real
# redis.asyncio client, which (a) avoids writing to a live shared Redis
# instance on every test run, and (b) avoids that client's connection pool
# binding to one pytest-asyncio event loop and raising "Event loop is
# closed" once a later test's fresh event loop tries to reuse it.
os.environ.setdefault("RATE_LIMIT_REDIS_URL", "")
```

placed alongside the existing `SENTRY_DSN`/`SENTRY_DSN_BROWSER` lines (`tests/conftest.py:30-33`). No other file needs to change — `backend/rate_limit.py`'s existing `_build_store()` fallback logic already does the right thing once the var is actually empty; it was only ever getting a truthy value because nothing blanked it first.

**VERIFY (for whoever implements this):** confirm `tests/test_rate_limit.py` (if it asserts anything about `_InMemoryStore` vs `_RedisStore` selection) stays green; run the full suite with a real `RATE_LIMIT_REDIS_URL` present in the shell environment (simulating a developer's `.env`) both before and after the fix to prove the "Event loop is closed" failure mode is actually eliminated, not just coincidentally absent; `graphify update .`.

### 36.2 Ordering

Independent of everything else in this queue — low urgency (only manifests locally, and only once enough async tests accumulate to trigger event-loop reuse), but cheap and low-risk to fix, and worth doing before it causes another confusing debugging session for whoever next runs the full suite on a machine with a real `.env`.

## 37. Findings from the import-chain repair session — five stacked `ImportError`s on `main`, all five now fixed, but `main` still does not fully import for three newly-discovered, previously-undocumented reasons (found 2026-08-25, rate-limiter fix landed 2026-08-25)

`main` did not import. Not "had a latent bug" — `python -c "import app"` and `python -c "import asgi"` both raised `ImportError` outright against the committed `HEAD` tree, which means the deployed serverless entrypoint could not have started. Five independent `ImportError`s were stacked on top of each other; because Python's import machinery stops at the first failure, each one hid the next, so the only way to find them was to fix one, re-run, and see what surfaced. All five were genuine "a symbol was deleted or moved but one caller was never updated" slips (the fifth being the larger, review-worthy Phase 9a migration below) and are now fixed and committed:

1. `5a018aa` — `backend/helpers.py` was missing `SECURITY_RESPONSE_HEADERS`. (This commit also carries the actual fix for the 2026-08-24 CSP incident: `clerk.shelah.org` and Cloudflare Turnstile were both blocked by the Content-Security-Policy, i.e. the §16.6 Phase 9c Turnstile gate could not have loaded in production.)
2. `e7d2064` — `backend/logging_setup.py` was missing `submit_with_context`, the contextvars-propagation wrapper for `ThreadPoolExecutor` submissions. Fully specified by tests that already existed and were already failing.
3. `871a100` — `backend/helpers.py` was missing `_resolve_client_ip` and `_coarse_ai_error_reason`.
4. `7a809b9` — `backend/routes_user.py` imported `require_clerk_auth` / `maybe_require_clerk_auth` from `app` instead of `backend.auth`.
5. `a519a21` — the Phase 9a rate-limiter unification landed as one reviewed commit: `backend/rate_limit.py` (new), `asgi.py`'s middleware registration, `backend/routes_devtools.py`'s corrected import block and `/api/stack/health` body, `tests/test_rate_limit.py` (new), `tests/test_rate_limit_config.py` (new). See §37.1's status line for detail.

**This closes every documented ImportError in the originally-diagnosed chain — but does not mean `main` imports today.** Verifying commit `a519a21` against a fresh `HEAD` export surfaced three further, previously-undocumented blockers, none of them related to the rate limiter, all discovered only because this pass actually re-ran `python -c "import app"` against committed `HEAD` rather than trusting that "the fifth ImportError was the last one." See §37.3.

**The meta-finding, stated plainly:** every one of these is a direct, now-materialized consequence of §34.1 / §28.1 — the uncommitted working tree that has been flagged across three prompts (Prompt 41 at ~150 paths, Prompt 46 at 181) and never resolved. Every affected file is one where the correct code exists in the working tree and only a stale, missing, or incomplete version was ever committed. §37.3's three new findings are more of the same pattern, not a new problem — the uncommitted-working-tree backlog is large enough that fixing one surfaced layer reliably reveals the next.

### 37.1 `backend/routes_devtools.py` at `HEAD` still imports the deleted Flask-Limiter symbols, and `backend/rate_limit.py` — the module that replaced them — was never committed at all, so §16.9's "Phase 9a shipped" reconciliation is true of the working tree and false of `main`

**Status: ✅ Fixed and committed 2026-08-25, commit `a519a21`.** Landed as one reviewed commit exactly as the "Fix" bullet below specifies — not a bare import-line patch. `python -c "import app"` / `python -c "import asgi"` both exit 0 against a fresh export of the new `HEAD` (this specific `ImportError` is gone for good), `tests/test_rate_limit.py` + `tests/test_rate_limit_config.py` pass (17 passed), and the full suite (`RATE_LIMIT_REDIS_URL` blanked per §36.1, bundled into the same commit's `tests/conftest.py` change) passes clean — exit 0, zero failure markers. The `_POLICIES` cross-module-boundary question raised in point 1 below was decided: left as a direct `rate_limit._POLICIES` read from `routes_devtools.py`, matching the working tree's existing choice, not promoted to a public accessor — a deliberate call, not an oversight, but worth revisiting if a second consumer ever needs the same data. **This does not mean `main` imports today** — see §37.3 for three further, unrelated blockers this verification pass surfaced.

**Severity:** Critical, deploy-blocking. This was the last *documented* thing standing between `HEAD` and an importable application — three further, undocumented ones surfaced once this was fixed; see §37.2.

**Symptom**, reproduced by exporting the committed `HEAD` tree into a clean scratch directory (not the working tree — that is the whole point) and running `python -c "import app"`:

```
ImportError: cannot import name 'limiter' from 'app'
...
RuntimeError: Blueprint 'routes_devtools' from 'backend.routes_devtools' failed to load: cannot import name 'limiter' from 'app'
```

**Root cause.** This is the tail end of the §16 Phase 9a rate-limiter unification. §16.8.1 diagnosed the pre-unification state — Flask-Limiter in `app.py` plus a second, independently-maintained in-process limiter in `asgi.py` for `/ask` only: two stores, two key functions, two 429 body shapes with nothing keeping them in sync, which is precisely the "live, divergent duplication" failure mode §2 and §16.3 name as this project's most dangerous anti-pattern, re-created inside the security layer. §16.3-L2 specified the replacement (one policy table, one store, one key function, one 429 shape, installed once as ASGI middleware so it observes 100% of traffic exactly once — Flask routes included, via the `WSGIMiddleware` mount). §16.9 records that as shipped on 2026-08-22. **Read those three sections rather than re-deriving any of this; the design rationale is settled and is not what is open here.** What is open is purely that the migration was verified against a working tree and then only partially committed.

The actual split, verified by direct inspection on 2026-08-25:

- **`backend/rate_limit.py` — untracked. Never committed, not once.** 350 lines, complete, with a module docstring that states the §16/§16.3-L2 design and the §2 constraint it honors (`This module must not import app`). Contains `RateLimitMiddleware`, the `_POLICIES` table, `_InMemoryStore` / `_RedisStore` behind one `_RateLimitStore` interface, the module-level `_store = _build_store()` singleton, `RATELIMIT_ENABLED`, `RATE_LIMIT_REDIS_URL`, and the `get_shared_store()` accessor §16.10's cost breaker reuses instead of opening a second Redis connection. Imports only `backend.auth`, `backend.helpers`, `backend.logging_setup`, and `starlette.*`.
- **`asgi.py` — correct in the working tree, uncommitted.** `from backend.rate_limit import RateLimitMiddleware` (line 34) and `fastapi_app.add_middleware(RateLimitMiddleware)` (line 144) are both already there. Nothing to design; this half just needs to land.
- **`app.py` — already fully migrated at `HEAD`, no working-tree change needed.** The `Limiter` construction, `RATE_LIMIT_ASK`, `RATE_LIMIT_DEFAULT` and friends are gone; all that remains is the explanatory comment at lines 809–821 pointing at `backend.rate_limit.RateLimitMiddleware` as the single enforcement point (and noting that bare `python3 app.py` therefore has no limiter of its own — deliberate, per §16.1 D1). `SUPABASE_SERVICE_ROLE_KEY` was likewise already renamed to `SUPABASE_SECRET_KEY` here (`app.py:861`).
- **`backend/routes_devtools.py` — the only straggler.** Its committed import block still asks `app` for `limiter`, `RATE_LIMIT_ASK`, `RATE_LIMIT_DEFAULT`, and `SUPABASE_SERVICE_ROLE_KEY`. **None of those four names exist in `app.py` anymore.** The first one Python reaches is `limiter`, which is why that is the message in the traceback; the other three are queued up behind it.

**Why this is not the one-line fix the other four were.** The corrected import block already exists in the working tree, and it is not "the same list minus `limiter`" — it is a different shape:

```python
from backend import cost_meter, rate_limit
from backend.auth import CLERK_ENFORCE_AUTH, maybe_require_clerk_auth, require_clerk_auth
from backend.helpers import _is_same_origin_request

from app import (
    app, DEVTOOLS_STATS, CLERK_PUBLISHABLE_KEY, CLERK_JWT_ISSUER,
    SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_PUBLISHABLE_KEY,
    SUPABASE_PREFS_TABLE, SUPABASE_USER_MEMORIES_TABLE,
    SUPABASE_STUDY_BOOKMARKS_TABLE, SUPABASE_ASK_HISTORY_TABLE,
    SUPABASE_ANSWER_FEEDBACK_TABLE, STRICT_SUPABASE_RLS, api_health,
    _get_supabase_client, _get_request_supabase_client,
    _extract_supabase_access_token, _get_request_user_id,
    _extract_client_ip, _capture_backend_error,
)
```

Four things are entangled in that diff, and a future session should verify each one against what the route handlers in the file actually call rather than pasting the block:

1. **`limiter` / `RATE_LIMIT_ASK` / `RATE_LIMIT_DEFAULT` are dropped, not renamed — but their *usage* was replaced, not deleted.** `/api/stack/health`'s `security` block (`backend/routes_devtools.py:66-76`) now reads `rate_limit.RATELIMIT_ENABLED`, `rate_limit.RATE_LIMIT_REDIS_URL`, and the live `rate_limit._POLICIES` table off the module, which is the concrete implementation of §16.9's "`/api/stack/health` no longer lies" bullet and closes §16.8.1's "an operator reading that endpoint gets a confidently wrong answer" finding. So this is a real behavioral change riding along with an import fix, and it deserves to be reviewed as one. Minor note for whoever reviews it: `_POLICIES` is a leading-underscore name being read across a module boundary; either accept that deliberately or promote it to a public accessor alongside the existing `get_shared_store()` — do not leave it ambiguous.
2. **`SUPABASE_SERVICE_ROLE_KEY` → `SUPABASE_SECRET_KEY`.** Already done in `app.py`; `routes_devtools.py:29,82` is the consumer side of the same rename.
3. **Newly imported names** (`_extract_supabase_access_token`, `_extract_client_ip`, `_get_request_supabase_client`, `STRICT_SUPABASE_RLS`, `cost_meter`) are all genuinely used — `:199`, `:275`, `:221`, `:202`, `:320` respectively — but confirm this by grep rather than trust, since an unused import here is the exact class of drift that created this section.
4. **Two untracked test files, `tests/test_rate_limit.py` (220 lines) and `tests/test_rate_limit_config.py` (75 lines), belong to the same uncommitted change.** `test_rate_limit.py` covers the store abstraction's `get`/`setex` and `get_shared_store()` (the §16.3-L3 / Prompt 29b surface); `test_rate_limit_config.py` is the `RATELIMIT_ENABLED` boot-time regression test. Both are now committed (`a519a21`), so **§16.9's citation of `tests/test_rate_limit_config.py` is accurate again** — no text change needed there, it was only ever stale relative to the uncommitted working tree, not wrong about what the file would cover. Neither file covers `RateLimitMiddleware`'s fail-open/fail-closed posture, so Prompt 43/§31.1's gap stays fully open — not narrowed by committing these. §31.1's stale citation of a nonexistent `tests/test_rate_limit_middleware.py` has been corrected in place to point at `tests/test_rate_limit.py` (the file that would host that coverage if added), with an explicit note that the gap remains open.

**Fix.** Land the whole Phase 9a change as one reviewed commit rather than patching the import line in isolation: `backend/rate_limit.py` (new file), `asgi.py`'s middleware registration, `backend/routes_devtools.py`'s corrected import block and `/api/stack/health` body, and both test files. Do **not** fix `routes_devtools.py` alone — importing `backend.rate_limit` from a tracked file while the module itself stays untracked converts this `ImportError` into a `ModuleNotFoundError` and leaves `main` exactly as non-deployable as it is now.

**Verify.** `python -c "import app"` and `python -c "import asgi"` both exit 0 against the *committed* tree, not just the working tree — export `HEAD` to a scratch directory and run them there, which is the check that would have caught all five of these. Then `pytest tests/test_rate_limit.py tests/test_rate_limit_config.py` green, then the full suite (blank `RATE_LIMIT_REDIS_URL` first, per §36.1 — which is now doubly relevant, since committing `backend/rate_limit.py` is what makes that leak reachable in CI). Then `graphify update .` and confirm no `backend/*` → `app` top-level edge was introduced.

### 37.2 Three further, previously-undocumented reasons `main` still does not fully import — found 2026-08-25 while verifying the §37.1 fix

**Severity:** Critical, deploy-blocking, same class as §37.1 — but **deliberately left unfixed in this pass.** Each of these is its own review-worthy change (untracked route files, a runtime dependency swap, a missing function backing a cron-triggered endpoint), not a one-line repair, and none was in scope for a session whose brief was specifically the rate-limiter unification. Fixing any of them here would repeat the exact mistake §37.1 itself documents: smuggling a larger, unreviewed change in under the banner of "making the imports work." Flagged here instead, following this document's own "findings from implementing X" convention, so they don't quietly disappear.

**37.2.1 — Status: ✅ Fixed and committed 2026-08-26, commit `9a7c159`.** Landed the four blueprint modules plus their 7 template dependencies (`about.html`, `help.html`, `glossary.html`, `ai-disclosure.html`, `acceptable-use.html`, `dmca.html`, `licenses.html`), `static/css/legal.css`, `static/data/glossary.json`, and `scripts/generate_glossary_json.py` — the exact closure needed for `import app` to succeed and for every new route to render without `TemplateNotFound`, discovered by iterating a clean-room `git archive HEAD` export rather than guessing. `routes_privacy.py`'s `cost_meter.expire_stale_budget_reservations` dependency was resolved by landing cost_meter.py's full surface first (commit `f81ace6`, see §37.2.3). **Deliberately not included**, and still uncommitted: the working tree's `terms.html`, `privacy.html`, `accessibility.html`, `index.html`, `components/legal_topbar.html`, `components/legal_scripts.html`, `components/icons.html` — these carry the real cross-links between the new legal pages plus an unrelated topbar visual refresh, are large (600–1900 line diffs each), and were judged too content-sensitive to wave through under an import-fix banner. Consequence: 3 of 7 parametrized cases in the new `tests/test_routes_legal.py::TestLegalPagesCrossLinking` (`/terms`, `/privacy`, `/accessibility`) fail against committed `HEAD` — expected and documented, not a regression to chase. See §38 for the follow-up.

**37.2.1 (original finding, for the record) — `backend/routes_legal.py`, `backend/routes_privacy.py`, `backend/routes_pages.py`, `backend/routes_feedback.py` are entirely untracked, yet `app.py`'s `_BLUEPRINTS` list at `HEAD` already registers all four.** Confirmed via `git ls-files` / `git status --porcelain` (all four report `??`) against `app.py`'s already-committed `_BLUEPRINTS` entries `("backend.routes_legal", "routes_legal")`, `("backend.routes_privacy", "routes_privacy")`, `("backend.routes_pages", "routes_pages")`, `("backend.routes_feedback", "routes_feedback")`. This means `import app` cannot fully succeed even on top of §37.1's fix — Flask's blueprint loader will hit `ModuleNotFoundError` on the first of the four it tries to import. `routes_privacy.py` (334 lines in the real working tree) goes one layer deeper: it needs `backend.cost_meter.expire_stale_budget_reservations`, which does not exist at `HEAD` — it belongs to the unshipped §20b budget-reservation feature. **Fix:** review and commit all four files as their own reviewed change (572+ lines combined, not something to wave through), resolving `routes_privacy.py`'s `cost_meter` dependency first or stubbing it out. Out of scope here.

**37.2.2 — Status: ✅ Fixed and committed 2026-08-26, commit `7cf767c`.** The working tree's `zoneinfo` migration landed as-is (325 insertions / 142 deletions — `from zoneinfo import ZoneInfo` replacing `pytz.timezone()` throughout, plus `tests/test_zmanim_engine_extra.py` coverage and `requirements.txt`'s already-correct pytz-free state). Verified against `.venv/bin/python3` specifically — this repo's own venv matches `requirements.txt` exactly and correctly lacks `pytz`, unlike the machine's global/homebrew `python3`, which has `pytz` installed and would have silently masked this exact bug.

**37.2.2 (original finding, for the record) — `backend/zmanim_engine.py` at `HEAD` still does `import pytz`, which is not installed.** `requirements.txt` deliberately dropped `pytz` (its own comment explains why: Python 3.9+'s stdlib `zoneinfo` replaces it, flagged as a SonarCloud security-audit item). The working tree already has `zmanim_engine.py` migrated to `from zoneinfo import ZoneInfo`, but that migration is uncommitted. This surfaces *earlier* in the import chain than §37.2.1 — `import app` hits this before it ever reaches the blueprint loader. **Fix:** commit the working tree's `zoneinfo` migration for this file. Out of scope here; flagged, not fixed, same reasoning as §37.2.1.

**37.2.3 — Status: ✅ Fixed and committed 2026-08-26, commit `f81ace6`.** Landed `cost_meter.check_daily_budget_and_alert()` (the cron-invoked global daily breaker) as part of the full, previously-verified-but-uncommitted §20a/§20b/§29b surface rather than cherry-picking just that one function: also includes `check_user_budget_and_enforce()` + `expire_stale_budget_reservations()` (§20b atomic per-user budget ceiling — a hard import-time dependency of `routes_privacy.py`, see §37.2.1), `backend/logging_setup.py`'s `bind_budget_reservation`/`get_budget_reservation`/`get_client_key`/`get_user_id` (needed by the above), and `backend/health_check.py`'s `record_success`/`record_failure`/`reset` methods plus `_PASSIVE_SERVICES` handling (needed for `tests/conftest.py`'s new `_reset_api_health` autouse fixture). `tests/test_cost_meter.py`, `tests/test_cost_meter_budget_atomicity.py`, `tests/test_cost_meter_pricing.py`, `tests/test_logging_setup.py` all landed alongside. One test-isolation bug found in an adjacent, still-untracked test file during this work — see §38.2.

**37.2.3 (original finding, for the record) — the newly-committed `routes_devtools.py`'s `/api/devtools/budget-check` route will raise `AttributeError` at runtime, and it's wired to a production cron job.** The route (landed as part of `a519a21`, §37.1) calls `asyncio.run(cost_meter.check_daily_budget_and_alert())`. That function does not exist in `backend/cost_meter.py` at `HEAD` — only `estimate_cost_usd`, `_insert_usage_row`, and `record_llm_call` do. `vercel.json` already has a cron entry configured to hit exactly this path. Landing `a519a21` as a whole file (rather than trimming this route out) was a deliberate choice — see §37.1's status note — matching this document's own instruction to land the full reviewed `routes_devtools.py` body rather than a further-minimized subset; the tradeoff is that this specific route is now live but broken. **Fix:** implement `cost_meter.check_daily_budget_and_alert()` (part of the unshipped §16.10/Phase 9b cost-breaker work) before the next cron firing, or temporarily disable the `vercel.json` cron entry. Genuine production risk, not fixed here — flagged for immediate follow-up given the cron will fire regardless of whether this document is read first.

### 37.3 Ordering — ✅ resolved 2026-08-26

**Status: closed.** All three of §37.2's blockers are now fixed and committed (`7cf767c`, `f81ace6`, `9a7c159`, landed in that order — zoneinfo migration first since it's earliest in the import chain, then cost_meter's surface since routes_privacy.py needs it, then the four blueprints last). Verified against a clean-room `git archive HEAD` export: `python -c "import app"` and `python -c "import asgi"` both exit 0 on the current `main` for the first time since this investigation began. The condition this section originally described — "nothing else in this document can be verified against a deployable tree" — no longer holds; `main` is importable and the full test suite (`RATE_LIMIT_REDIS_URL` blanked, §36.1) passes clean except the 3 known/deliberately-out-of-scope `TestLegalPagesCrossLinking` failures documented in §37.2.1 and carried forward as §38.1.

It also had a natural sequencing relationship with §34.1: this was a strict subset of that uncommitted backlog, scoped down to the slice that was deploy-blocking. Closing it first — 3 commits, ~20 files, all with a single clear justification ("`app.py`/`asgi.py` already reference this at `HEAD`") — is exactly the kind of per-prompt-granularity commit §34.1's own recommended plan asked for, and makes the remaining backlog smaller and less frightening without having pre-empted §34.1's broader "how do we break the rest into reviewable commits" question, which is still open.

**Original framing, for the record:** *"Do this first, ahead of everything else in the queue including §35.1's severity framing and §34.1's backlog cleanup. Those describe defects in a running application; §37.1 (fixed) and §37.2 (open) are collectively the reason there may not be a running application at all."*

## 38. Findings from closing §37.2 (found 2026-08-26) — two follow-ups, one content decision and one test bug, deliberately left open

Two findings surfaced while closing out §37.2's three deploy-blocking gaps (commits `7cf767c`, `f81ace6`, `9a7c159`). Neither is deploy-blocking — `main` imports and the full suite is green — so, per this document's own established practice (§26, §27, §28, §29, §31...), each is filed here rather than folded into the commits that found it.

### 38.1 Legal-page cross-linking is real, reviewed working-tree content, not an import fix — still uncommitted, 3 tests document the gap

**Severity:** Low — cosmetic/content, not functional. No route is broken; the pages §37.2.1 committed (`/ai-disclosure`, `/acceptable-use`, `/dmca`, `/licenses`, `/about`, `/help`, `/glossary`) all render correctly on their own. What's missing is that the *existing* pages (`/terms`, `/privacy`, `/accessibility`) don't yet link to them.

**What's uncommitted:** `templates/terms.html`, `templates/privacy.html`, `templates/accessibility.html`, `templates/index.html`, `templates/components/legal_topbar.html`, `templates/components/legal_scripts.html`, and a new `templates/components/icons.html` — six modified + one untracked, each a 600–1900 line diff. These add the actual cross-links between all legal pages, plus (based on the `icons.html` split and topbar diff size) an apparently unrelated visual refresh of the shared topbar riding along in the same working-tree state. That bundling is itself worth flagging to whoever picks this up: it should probably be reviewed and possibly split into "add cross-links" vs. "refresh topbar visuals" rather than landed as one commit, since they're independently reviewable and a topbar visual change is exactly the kind of thing that benefits from its own screenshot-backed review per this project's UI/UX Pro Max standard (`.agents/ENGINEERING_RULES.md`).

**Concrete, currently-failing evidence:** `tests/test_routes_legal.py::TestLegalPagesCrossLinking`, parametrized over `ALL_LEGAL_PATHS = ["/terms", "/privacy", "/ai-disclosure", "/acceptable-use", "/dmca", "/accessibility", "/licenses"]`, asserting every page links to every other. 3 of 7 cases fail against committed `HEAD` today: `/terms`, `/privacy`, `/accessibility` (the three pre-existing pages whose committed templates don't yet reference the four new ones). The other 4 pass, since the newly-committed templates already include the links forward.

**Fix:** review `templates/terms.html`, `templates/privacy.html`, `templates/accessibility.html`, `templates/index.html`, `templates/components/legal_topbar.html`, `templates/components/legal_scripts.html`, `templates/components/icons.html` as their own change — split into a content/cross-linking commit and (if confirmed unrelated) a separate topbar-visual-refresh commit. Verify against the UI/UX Pro Max checklist (contrast, touch targets, `prefers-reduced-motion`) given the topbar's visual scope, and confirm all 7 `TestLegalPagesCrossLinking` cases go green.

**VERIFY (for whoever implements this):** `pytest tests/test_routes_legal.py -q --no-cov` fully green (all 7 parametrized cases, not just the 4 passing today); manual pass through both light and dark themes on `/terms`, `/privacy`, `/accessibility`, and the topbar specifically, per this repo's `.agents/ENGINEERING_RULES.md`; `graphify update .`.

### 38.2 `tests/test_cost_meter_call_sites.py` has an order-dependent test-isolation bug — passes in the full suite, fails in isolation

**Severity:** Low — the file is untracked (never shipped, so nothing in production or CI is affected today), and the code it tests (`backend/claude.py`) is unmodified, already-committed, and itself correct. This is purely a bug in the *test's* isolation, not a product bug.

**Root cause:** `backend/claude.py` lazily imports the `google-genai` SDK on first use — `_ensure_genai_loaded()` sets module-level `genai`/`genai_types` globals and a `_genai_loaded` flag, called from `_configure_gemini_client()` (`backend/claude.py:485`). `_call_gemini_httpx_model` (`:1951`) checks `if not genai_types: return {..., "error": "gemini_sdk_missing", ...}` *before* it ever reaches the `record_llm_call(...)` call at `:2017` — so if `genai_types` is still `None` when this path runs, the call short-circuits and `record_llm_call` (and therefore the test's `captured_cost_calls` monkeypatch target) is never invoked. `tests/test_cost_meter_call_sites.py`'s `TestAsyncGeminiPrimaryCallSite`/`TestSyncGeminiPrimaryCallSite` classes monkeypatch `_configure_gemini_client` with a no-op lambda — which is exactly the function whose real body performs the one-time `_ensure_genai_loaded()` side effect. Run this file alone, the module-level `_genai_loaded` cache is still `False`, the no-op lambda skips loading it, `genai_types` stays `None`, and `captured_cost_calls` ends up empty (`assert 0 == 1`). Run it as part of the full suite, some earlier test has already triggered `_ensure_genai_loaded()` once (module-level state persists across the whole `pytest` process), so the cache is warm and the test passes — confirmed empirically both ways.

**Fix:** either (a) monkeypatch `_call_gemini_httpx_model`'s dependency more precisely — patch `genai_types` directly (or the specific client call) rather than `_configure_gemini_client` wholesale, so the lazy-load side effect isn't bypassed, or (b) call the real `_ensure_genai_loaded()` (or set the module globals directly) in the test's setup before monkeypatching `_configure_gemini_client`. Option (a) is more robust against future changes to `_configure_gemini_client`'s internals; option (b) is the smaller diff. Whoever picks this up should also run `pytest tests/test_cost_meter_call_sites.py -p no:randomly -q --no-cov` in isolation before and after to confirm the fix, since order-dependent bugs are exactly the kind that reappear if verified only via the full suite.

**VERIFY (for whoever implements this):** `pytest tests/test_cost_meter_call_sites.py -q --no-cov` green *in isolation* (not just as part of the full suite — that's the whole point); full suite still green afterward; `graphify update .`.

### 38.3 Ordering

Neither finding blocks anything else in this document — `main` imports, the full suite (minus the 3 documented `TestLegalPagesCrossLinking` cases) is green, and both items here are scoped, independent, low-severity cleanups. No particular urgency or sequencing between 38.1 and 38.2; either can be picked up first.

## 39. Account-deletion completeness — verify "delete my account" removes *all* of one user's data and *none* of anyone else's (requested by Akiva, 2026-08-26)

Not a bug report — a requested audit of `/api/user/delete-account` (`backend/routes_privacy.py`, shipped under §8.D, `claude_code_prompts.md` Prompt 16) against two properties: **complete** (every row this app ever wrote for that user is gone) and **isolated** (no other user's row is ever touched). Read `docs/PRIVACY_OPERATIONS.md` §1/§3 for the existing DSR policy language before starting; this section extends it, not replaces it. Verified by direct code inspection 2026-08-26, not assumed.

**The good news first — isolation is already correct, and it's correct by construction, not by convention.** Both `_export_table_rows()` and `_delete_table_rows()` (`routes_privacy.py:86-113`, `:148-159`) scope every query with `.eq("user_id", user_id)`, and `user_id` in both `export_user_data()`/`delete_account()` comes from exactly one place: `g.clerk_claims["sub"]`, set by `require_clerk_auth` after verifying the caller's own Clerk JWT (`backend/auth.py`). There is no route parameter, query string, or request-body field anywhere in this file that lets a caller name a `user_id` other than their own — a caller cannot delete or export anyone else's data by construction, not merely by discipline. Nothing to fix here; recorded so a future reader doesn't have to re-derive it (matching this document's practice of keeping verified-clean findings on record, e.g. §33.3/§33.7).

### 39.1 `answer_feedback` is written with a real `user_id` but excluded from both export and delete

**Severity:** Medium — a real, currently-orphaned personal-data table, not a hypothetical.

`_USER_DATA_TABLES` (`routes_privacy.py:53-59`) lists five tables: preferences, bookmarks, ask_history, memories, ai_usage_log. `backend/routes_feedback.py`'s `submit_feedback()` (`:48-63`) inserts into a sixth table, `SUPABASE_ANSWER_FEEDBACK_TABLE` (`answer_feedback`, `app.py:872`), with `"user_id": _get_request_user_id()` — populated with the real Clerk `sub` whenever the submitter is signed in (the route accepts signed-out feedback too, via `maybe_require_clerk_auth`, but authenticated submissions carry the real identity) plus a free-text `comment` field (`routes_feedback.py:56`, up to 500 chars). This table is in neither `export_user_data()` nor `delete_account()`'s loop — a user who deletes their account today keeps a row in `answer_feedback` forever, undiscoverable via the self-serve export and unreachable by the delete endpoint, with no Clerk identity left to authenticate a manual follow-up request.

**Fix:** add `("feedback", SUPABASE_ANSWER_FEEDBACK_TABLE)` to `_USER_DATA_TABLES` and import `SUPABASE_ANSWER_FEEDBACK_TABLE` from `app` in `routes_privacy.py` (already exported there, already imported the same way by `routes_devtools.py:35` and `routes_feedback.py:19` — no new plumbing). Because both `export_user_data()` and `delete_account()` iterate the same tuple, this one-line change closes the gap in both endpoints simultaneously. Confirm no other `app.py`-defined `SUPABASE_*_TABLE` constant is missing the same way — `_USER_DATA_TABLES` should be checked against every table any route ever writes a `user_id`-keyed row into, not assumed complete from its current five entries (this is exactly the class of drift §23 exists to catch generally; this is one concrete instance of it).

**VERIFY:** a new or extended test asserting `SUPABASE_ANSWER_FEEDBACK_TABLE` appears in `_USER_DATA_TABLES`; an integration-style test that inserts a feedback row for a fake user, calls `delete_account()`, and asserts the row is gone.

### 39.2 No cleanup is triggered if the Clerk identity is ever deleted by any path other than this app's own endpoint — the completeness guarantee is currently endpoint-shaped, not identity-shaped

**Severity:** High — this is the one that can silently defeat "complete," permanently, for any user it happens to.

`delete_account()` is the *only* code path in this repository that deletes Supabase rows for a user. Nothing in this codebase listens for a Clerk user actually being deleted. That distinction matters because Clerk identities can be deleted through more doors than this one:

1. **`templates/index.html:4106-4118`'s `openClerkUserProfile()`** calls `window.Clerk.openUserProfile()` — Clerk's own hosted account-management UI, embedded directly in this app. Clerk's `<UserProfile />` component ships a self-service "Delete account" control in its Security section, gated by a Clerk Dashboard setting whose current value for this app's Clerk instance is **not recorded anywhere in this codebase** — the same "dashboard setting, no versioned artifact" shape already found twice in this document (§21.2.1's Third-Party-Auth toggle, resolved 2026-08-24; §33.8's WAF rules, resolved 2026-08-26). If it is enabled, a signed-in user can delete their own Clerk identity through Clerk's UI without ever calling `/api/user/delete-account` — every row in `_USER_DATA_TABLES` (and, until §39.1 lands, `answer_feedback`) survives forever with no trigger left to clean it up, since the one signal that would have authorized a retry (a valid Clerk JWT for that user) is now gone too.
2. **An operator deleting a user directly from the Clerk Dashboard** — for a banned or abusive account, a support request handled by hand, or any future admin tooling — hits Clerk's API directly and has the identical orphaning effect. This path exists regardless of the setting in (1) and cannot be disabled; it is a normal, expected part of operating a Clerk-backed app.

Both are real operational paths, not edge cases invented for this audit — (2) in particular is the kind of action a solo operator will take the first time abuse or a takedown request needs handling, precisely the moment data-deletion correctness matters most.

**Fix — make deletion identity-shaped, not endpoint-shaped: add a Clerk webhook listener for the `user.deleted` event.** Clerk delivers webhooks via Svix; the standard, already-battle-tested pattern is a new route (per `.agents/ENGINEERING_RULES.md`, belongs in `backend/`, e.g. `backend/routes_webhooks.py`) that verifies the Svix signature against a `CLERK_WEBHOOK_SIGNING_SECRET` env var, and on a `user.deleted` event runs the same table-cascade (`_delete_table_rows` over `_USER_DATA_TABLES`, once §39.1 lands) keyed on the event payload's `data.id`. This closes both doors at once: it fires whether the identity was removed by this app's own endpoint (idempotent — `delete_account()` already deleted the rows, the webhook's second pass deletes zero more, matching the existing idempotency note at `routes_privacy.py:150`), by Clerk's own UI, or by dashboard admin action. **Do not skip the Clerk-side identity delete in `delete_account()` in favor of relying on the webhook alone** — keep both: the webhook is the completeness backstop for the doors this app doesn't control, the existing endpoint remains the one that gives the *user* an immediate, synchronous, self-serve confirmation (`response["ok"]`) rather than a fire-and-forget webhook with no reply channel.

**Operator action required alongside the code fix (same class as §21.2.1/§33.8):** check the Clerk Dashboard for whatever setting currently controls self-service account deletion from `<UserProfile />`, and record the outcome — enabled / disabled / not applicable at this plan tier — in `docs/SECURITY.md`, dated. If it's enabled and the webhook above isn't built yet, that combination is the specific, exploitable-by-the-user-themselves version of this gap and should be treated as the more urgent half to close first.

**VERIFY:** a test that simulates a `user.deleted` webhook payload (valid Svix signature) against a fake user with rows in every `_USER_DATA_TABLES` entry, asserting all rows are gone; a test asserting an invalid/missing signature is rejected (this is an unauthenticated-by-Clerk-JWT endpoint by necessity — Svix signature verification *is* its auth, get this test in before shipping); confirm the webhook is idempotent against a replayed event (Svix and most webhook providers deliver at-least-once, not exactly-once).

### 39.3 Cryptographic erasure — real technique, not the right fix for this app's current shape; here's what actually is

Akiva's question, answered directly: **crypto-shredding is a genuine, widely-used best practice — for erasing data trapped in copies you cannot individually `DELETE` from.** It works by encrypting a user's data with a per-user key at write time and, on deletion, destroying only the key — every ciphertext copy of that data, including ones sitting in backups or replicas the deletion logic never touches, becomes permanently unreadable without needing to reach each copy. It is the standard answer for immutable backups, WORM/archive logs, and data-warehouse replicas that outlive an app's own retention window.

**Does this app have that problem today? Only partially, and not in the place crypto-shredding would help.**

| Store | What actually happens on delete | Does crypto-shredding add anything here? |
|---|---|---|
| **Supabase Postgres, live tables** (`_USER_DATA_TABLES` + `answer_feedback` once §39.1 lands) | A real `DELETE ... WHERE user_id = ...` (§39 above) — the row is gone, not re-encrypted-and-unreadable-but-still-present. | **No.** A real `DELETE` is strictly stronger than crypto-shredding for a store you can write to directly; shredding only matters where `DELETE` isn't reachable. |
| **Clerk identity** | A real `DELETE` via Clerk's API (`_delete_clerk_user`, `routes_privacy.py:162-202`). | **No,** same reason — Clerk's platform, but the deletion is a real API-level delete, not a reversible soft-delete this app controls. |
| **Supabase platform backups / point-in-time recovery** | Provider-managed; this app has no row-level control over what a restored backup contains, and **this project's actual backup/PITR retention window is not documented anywhere** — genuinely unknown as of this audit. | **Yes, in principle** — this is exactly the class of copy crypto-shredding exists for. But the exposure it would close is *time-bounded* either way: once a backup rolls past Supabase's own retention window, the pre-deletion row is gone from there too, encrypted or not. Shredding narrows an already-bounded window; it does not close an otherwise-permanent one, which §39.1/§39.2 are. |
| **Sentry** — at least 10 of `backend/`'s 45 `_capture_backend_error(...)` call sites (grep-verified 2026-08-26) pass the **raw** Clerk `sub` as event/breadcrumb context (e.g. `routes_privacy.py:109-111`, `:156-158`, itself). `delete_account()` never touches Sentry. | The raw identity persists in historical Sentry events for however long this project's Sentry plan retains events — also undocumented here. | **No** — crypto-shredding is the wrong tool for third-party SaaS event data you don't hold the ciphertext or the store for. The actual fix is not sending the raw value in the first place. |

**Recommendation: do not build field-level per-user encryption/crypto-shredding right now.** It would mean encrypting every content-bearing column (`ask_history`'s question/answer, `user_memories`, `semantic_bookmarks`' notes/segment text, `answer_feedback`'s comment) behind a per-user key, decrypting on every read, and building key lifecycle management (issuance, rotation, guaranteed destruction-on-delete, and ensuring key backups never outlive the data they guard) — a cross-cutting architecture change to every user-content table, for a benefit that's bounded by a backup-retention window this project doesn't even have a documented number for yet. That's a large, permanent cost for a narrow, already-time-bounded gain, and it would also complicate anything server-side that ever wants to filter or search that text (e.g. a future full-text search over `ask_history`).

**Do instead, in priority order:**
1. **§39.1 and §39.2 first.** Both are *unbounded* gaps — an orphaned `answer_feedback` row or a Clerk-side-deleted-but-never-cleaned-up user's rows sit there forever, not just until a backup rotates out. Closing an unbounded leak matters more than shrinking a bounded one.
2. **Look up and document Supabase's actual backup/PITR retention window** in `docs/PRIVACY_OPERATIONS.md`, dated. Most privacy-policy deletion language is phrased as "within N days," not "instantly and irreversibly everywhere" — this gives that N a real, provider-verified number instead of an implicit, unverifiable claim.
3. **Hash `user_id` before it reaches Sentry**, mirroring the pattern this project already uses for rate-limit/Turnstile mitigation logging (`log_mitigation(..., key_hash, ...)`, §8's "hash the key, never a raw IP or Clerk sub" rule). This is a small, code-level fix — not a re-architecture — and it closes a real, currently-unbounded leak (Sentry's own retention decides how long the raw identity lingers, and this app has never set a shorter one). Grep every `_capture_backend_error` call site for a raw `user_id`/`"sub"` value and hash it the same way the mitigation-logging path already does.

**Also checked, no action needed:** the rate-limiter's identity-aware keys (`backend/rate_limit.py`, keyed on Clerk `sub` per §16.6) are TTL-bound counters in the shared store, not persistent rows — they self-expire within their own window regardless of account deletion, and are not a completeness gap.

### 39.4 Exit criteria

- `answer_feedback` included in both `export_user_data()` and `delete_account()`; a test proves it (§39.1).
- A `user.deleted` Clerk webhook exists, is Svix-signature-verified, is idempotent, and is covered by tests for both the happy path and an invalid-signature rejection (§39.2).
- The Clerk Dashboard's self-service account-deletion setting is checked and the outcome recorded with a date in `docs/SECURITY.md` (§39.2, operator action).
- Supabase's backup/PITR retention window is looked up and documented in `docs/PRIVACY_OPERATIONS.md` (§39.3).
- `user_id` is hashed, not raw, at every `_capture_backend_error` call site that currently passes it (§39.3).
- Full `pytest -q` green; `graphify update .`.

### 39.5 Ordering

§39.1 and §39.2 are independent of each other and of everything else currently open in this document — do both before or alongside anything else in the queue, since each hour a Clerk-side deletion could occur (§39.2, point 2 especially — an operator handling abuse/takedown by hand is a completely ordinary action, not a rare edge case) is another hour a real user's data could go permanently unreachable. §39.3's two action items (backup-window lookup, Sentry hashing) are lower-urgency and can be scheduled independently of 39.1/39.2 and of each other.
