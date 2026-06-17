# Sh'elah — Optimization Roadmap (`plan.md`)

**Date:** 2026-06-09 · **Status:** Proposal only — no code changes executed.
**Scope:** Full workspace scan including `.agents/` and `.claude/`, excluding `.git/`, `.venv/`, `.env`, `__pycache__`.

---

## 0. Executive summary

Sh'elah is a Flask app (5,023-line `app.py`, 48 routes) wrapped by a FastAPI ASGI shell (`asgi.py`) that re-implements `/ask` asynchronously, deployed on Vercel. The frontend is a 11,202-line `templates/index.html` (~9,500 lines of inline `<script>`) plus a partially-extracted ES-module layer in `static/js/`. The biggest risks and wins, in order:

1. **`app.py` monolith** — decompose into blueprints (highest leverage, highest care required).
2. **`templates/index.html`** — finish migrating inline JS into `static/js/` modules; it currently dwarfs the module layer 50:1.
3. **Duplication between `app.py` `/ask` and `asgi.py` `/ask`** — two parallel implementations of the same pipeline will drift.
4. **Sync `requests` usage inside code reachable from the async path** — event-loop safety relies on `asyncio.to_thread` discipline; formalize it.
5. **Serverless statefulness** — in-process caches, rate limiters, and `DEVTOOLS_STATS` don't survive or share state across Vercel invocations.

Completed in this pass (per your instruction): trash files moved to `_workspace_backups_and_trash/`; styling/engineering directives written to `.agents/ENGINEERING_RULES.md` and `.claude/CLAUDE.md`. Everything below is **proposed, not executed**.

---

## 1. Workspace organization (done)

Moved to `_workspace_backups_and_trash/`:
`test_results.txt`, `LOCALHOST_AUDIT_REPORT.md`, `SYSTEM_PROMPTS_AUDIT.md`, `test_sefaria.py`, `test_sefaria_2.py`.

**Follow-up proposals (not executed):**
- `reports/library_leaf_remove_fix_report.full.json` is committed but its siblings are gitignored — move it to the trash folder or ignore it too.
- `reports/browser_qa/final_audit_2026-05-05/AUDIT_RESULTS.md` — historical; candidate for the trash folder.
- `docs/` contains 17 files, several of which are point-in-time audit artifacts (`AUDIT_SUMMARY.md`, `ERROR_AUDIT_REPORT.md`, `SYSTEM_PROMPT_REFACTORING*.md`, `INTEGRATION_*`). Propose a `docs/archive/` split so living docs (`SERVICE_ARCHITECTURE.md`, `DATABASE.md`, `QUICK_START.md`, `MANIFEST.md`, `SOURCES_REGISTRY.md`) stand out.
- Scattered `.DS_Store` files everywhere — `.gitignore` covers them, but run `git rm --cached` if any are tracked, and consider a cleanup sweep.
- `.gitignore` has duplicated blocks (`.venv/`, `__pycache__/`, `.env` appear twice) and the blanket `.*` / `**/.*` rule is aggressive — it silently ignores `.pre-commit-config.yaml` unless git already tracks it. Deduplicate and add explicit `!.pre-commit-config.yaml`, `!.gitignore`, `!.env.example`.

---

## 2. System directives (done)

- **`.agents/ENGINEERING_RULES.md`** (new): full UI/UX Pro Max + Framer Motion + async-safety rule set.
- **`.claude/CLAUDE.md`** (updated): condensed mandatory summary referencing the rules file, preserved graphify section untouched.
- `.claude/settings.json` (graphify hooks) and `.claude/skills/graphify/` were read for context and deliberately left unmodified — they are tool plumbing, not style policy.
- `.agents/skills/supabase*` are vendored third-party skills; left unmodified by design (edits would be overwritten on skill update).

---

## 3. File-by-file critique and roadmap

### 3.1 `app.py` (5,023 lines, 48 routes) — CRITICAL

**Problems**
- God module: routing, auth (Clerk JWT), Supabase clients, RAG retrieval, translation, glossary lookup, holiday/calendar formatting, AI answer normalization, devtools, and caching all live here. ~90 private helpers.
- `asgi.py` reaches into it for private functions (`_verify_clerk_token`, `_build_ask_tool_context`, `_retrieve_community_knowledge`, `_compose_answer_with_prefixes`, …) — a fragile implicit API; renaming anything in `app.py` silently breaks the async path.
- In-process state (`DEVTOOLS_STATS`, `_rate_limit` caches, ask-payload cache via `_bounded_cache_set`) is per-instance; on Vercel serverless these reset per cold start and are not shared across instances — stats and rate limits are best-effort at best. Document this or move counters to Supabase.
- Translation helpers (`_translate_text_google`, `_translate_text_mymemory`, retries with a 2.5s budget in `_fill_missing_english_lines`) are blocking and run inside request handlers — fine under WSGI threads, dangerous if ever called from the async path without `to_thread`.
- Two `ThreadPoolExecutor` usages (lines ~3306, ~3320) create a pool per request — pool creation is cheap-ish but a module-level bounded executor is cleaner and avoids thread churn under load.

**Roadmap (phased, zero-breakage — see §5)**
1. *Extract, don't rewrite:* create `backend/auth.py` (Clerk/Supabase token logic), `backend/rag.py` (knowledge/memory retrieval + scoring), `backend/answers.py` (normalization/attribution/prefix helpers), `backend/translate.py`, `backend/glossary.py`, `backend/holidays.py`. Move functions verbatim; leave `from backend.auth import _verify_clerk_token` re-import shims in `app.py` so `asgi.py` and any other consumer keep working unchanged.
2. Split routes into Flask blueprints: `routes_library.py`, `routes_prayers.py`, `routes_community.py`, `routes_calendar.py`, `routes_user.py`, `routes_devtools.py`. Register all in `app.py`; URL surface unchanged.
3. Promote the per-request `ThreadPoolExecutor`s to one module-level executor with a hard `max_workers` cap.
4. Replace ad-hoc dict caches with a single small TTL-cache utility (one implementation, reused by `search.py`, `sefaria_library.py`, `zmanim_engine.py`, `app.py` — there are at least 6 hand-rolled cache variants in the repo today).
5. Add type hints progressively to extracted modules only (no churn in untouched code).

### 3.2 `asgi.py` (498 lines) — HIGH

**Strengths:** correct use of `asyncio.to_thread` for blocking calls, parallel `asyncio.gather` fan-out, bounded in-process rate limiter, graceful fallback ladder.

**Problems**
- **Pipeline duplication** with Flask `/ask` (line 3213 in `app.py`): the strict-mode block, attribution notes, fallback handling, and memory storage exist twice. Any prompt or policy change must be made in two places.
- Rate limiter eviction pops the *oldest-inserted* key, not least-recently-used — an attacker cycling 2,048 IPs evicts active users' counters. Use an LRU (`OrderedDict.move_to_end`) or accept and document.
- Also serverless-stateful: per-instance limiter means the effective limit is 20/min × N instances.
- `_collect_primary_sources` instantiates a fresh `ShelahEngine()` per ref inside `_load_one` — if construction is non-trivial, hoist one engine per request (or module-level if thread-safe).
- The hardcoded prayer-keyword early-return (`"Shacharit" in question` …) is case-sensitive English-only string matching inside the handler; move to a helper shared with the Flask route.
- Broad `except Exception: return None` in `_load_one` and `_extract_user_id_from_bearer` swallows diagnostics; log at debug level at minimum.

**Roadmap:** extract a transport-agnostic `backend/ask_pipeline.py` with one async implementation; the Flask route becomes `asyncio.run`/thread-bridge or is deprecated in favor of the ASGI route (Vercel already routes everything to `asgi.py`, so the Flask `/ask` is likely dead code in production — verify, then remove in phase 3).

### 3.3 `backend/claude.py` (1,480 lines) — HIGH

- Mixes prompt construction, sanitization/security validation, structured-output parsing, sync Anthropic SDK calls, async httpx calls for both Anthropic and Gemini, and Gemini summarization. Split into `prompts.py`, `security.py`, `model_clients.py` (sync+async), `structured.py`.
- Duplicate model-calling logic: `_call_claude_model` (sync SDK) vs `_call_anthropic_httpx_model` (async raw HTTP) must keep headers, retries, and parsing in sync manually. Consider standardizing on the official SDK's `AsyncAnthropic` to delete the hand-rolled httpx client.
- Verify all httpx async calls set explicit timeouts and reuse a module-level `httpx.AsyncClient` (per-call clients lose connection pooling).
- Regex/keyword security validators (`_detect_out_of_scope_subject`, injection markers) are fine as defense-in-depth; document that they are heuristics, and keep them centralized in the new `security.py`.

### 3.4 `backend/sefaria_library.py` (1,372 lines) — MEDIUM

- Hand-rolled `_cache` dict with TTL: confirm there's a size bound (unbounded dict in a long-lived process is a slow leak; on serverless it's moot but local dev runs long). Replace with the shared TTL-cache utility from §3.1.4.
- Hardcoded `sefaria.org.il` host — make `SEFARIA_API` env-configurable with the current value as default (resilience if the .il mirror degrades).
- Sync `requests.Session` is fine for the Flask path; the async path reaches it only via `to_thread` (good) — codify this with a module docstring rule: "never import into async code without to_thread".
- `deepcopy` on cached payloads can be hot for large texts; measure, and consider returning immutable views or copying only mutated subtrees.

### 3.5 `backend/sefaria.py` (272 lines) — LOW

- Curated `TOPIC_REFS` mapping is good design. Move the data block to `backend/data/topic_refs.json` (or keep in-code but separate `topic_refs.py`) so logic and data evolve independently.
- `_DAILY_STUDY_CACHE` — same shared-TTL-cache consolidation.

### 3.6 `backend/search.py` (376 lines) — LOW

- Already has both sync and async (httpx) connectors — good. Three parallel cache dicts (`_WIKI_CACHE`, `_HALACHIPEDIA_CACHE`, `_HEBREWBOOKS_CACHE`) → one keyed cache utility.
- Confirm `async_search_*` functions share one `httpx.AsyncClient` and set timeouts; per-call clients negate pooling.
- HTML parsing via regex + `unescape` is fragile for Halachipedia; acceptable for best-effort enrichment but wrap with strict output caps.

### 3.7 `backend/zmanim_engine.py` (456 lines) — MEDIUM

- `TimezoneFinder()` at module import is correct (expensive init done once) — keep.
- Hebcal day/month caches: TTL consolidation as above; ensure cache keys include coordinates rounding so nearby users share entries deliberately, not accidentally.
- Verify no naive/aware datetime mixing between `pytz`, `pyluach`, and `zmanim` lib (classic source of off-by-one-day bugs around DST transitions and halachic midnight) — add focused unit tests for DST boundary dates.

### 3.8 `backend/health_check.py` (206 lines) — LOW

- Circuit breaker with `threading.Lock` is sound for the threaded WSGI path. Note: the lock is held only in-process; per-instance breakers on serverless mean each cold instance re-probes failing services. Acceptable — document it.

### 3.9 `backend/customs.py`, `data_service.py`, `calendar_service.py`, `logging_setup.py` — LOW

- Small and focused; mostly fine. `customs.py` should validate `customs/*.json` schemas at load (one malformed community file currently can raise at request time). Add a startup validation pass with a clear error naming the offending file.
- `backend/__init__.py` is 1 line — fine.

### 3.10 `templates/index.html` (11,202 lines; ~9,500 in `<script>`) — CRITICAL (frontend)

- This is the single largest maintainability liability after `app.py`. 10 `<script>` blocks and 2 `<style>` blocks inline; meanwhile `static/js/` modules total only ~520 lines. The migration to ES modules (`state.js` pub/sub, `main.js` bootstrap) has started — finish it.
- **Roadmap:** carve inline code into modules by feature in this order (lowest coupling first): zmanim/calendar UI → library/reader → prayers → AI ask panel → settings/auth glue. Each extraction: move code verbatim into a module, export an `install*()` initializer, import from `main.js`, delete the inline block, smoke-test the route. One feature per commit.
- Inline `<style>` blocks duplicate concerns already split into `static/css/*.css` — migrate them into the matching sheet.
- Apply the new skeleton-state rule: AI answer area, library text panes, and calendar grid need layout-stable skeletons (several currently rely on spinners or blank regions — verify each async region during extraction).

### 3.11 `static/js/*.js` (5 modules, ~520 lines) — GOOD baseline

- `state.js` (pub/sub store), `ai-service.js`, `reader-ui.js`, `zmanim.js`, `main.js` — clean ES modules, `window.ShelahModules` escape hatch for the inline legacy code. Keep that bridge until index.html extraction completes, then delete it.
- Add JSDoc types and a tiny ESLint config (no build step needed) to lock style before the module count grows.

### 3.12 `static/style.css` (4,086 lines) + `static/css/*` (7 sheets, ~2,200 lines) — MEDIUM

- `style.css` predates the per-feature split; it certainly overlaps `css/sidebar.css`, `css/reader.css`, etc. Audit for dead selectors (use Chrome coverage tooling), then migrate sections into the feature sheets until `style.css` holds only tokens, reset, and layout shell.
- Centralize design tokens (colors, spacing scale, type ramp) as CSS custom properties in one `tokens.css` — prerequisite for the 4px/8px alignment and AA-contrast rules now mandated in `.agents/ENGINEERING_RULES.md`.
- Add `prefers-reduced-motion` guards around any existing transitions/animations.

### 3.13 `static/service-worker.js`, `manifest.webmanifest`, `offline.html` — LOW

- Verify the SW cache version is bumped on deploy (stale-asset bugs are the #1 PWA support issue). Propose embedding a build hash in the cache name. Confirm `offline.html` is precached and the fetch handler never caches `/ask` or auth responses.

### 3.14 `templates/terms.html`, `privacy.html`, `components/*` — LOW

- Fine. Shared `legal_topbar.html`/`legal_scripts.html` componentization is the right pattern — extend it to the main app shell when index.html is decomposed.

### 3.15 `vercel.json` — MEDIUM

- Minimal catch-all to `asgi.py` works, but: add explicit static routing so `/static/*` is served by Vercel's CDN instead of invoking the Python function for every asset (cost + latency win):
  ```json
  { "src": "/static/(.*)", "dest": "/static/$1" }
  ```
  before the catch-all. Also consider `functions` config for memory/duration limits on the AI route. **Test on a preview deployment first** — route config errors take the whole site down.

### 3.16 `requirements.txt` — MEDIUM

- Both `flask` and `fastapi` + `uvicorn` are required by the hybrid architecture — fine for now; phase 3 of the `/ask` consolidation may let Flask-Limiter go if remaining routes move to FastAPI (long-term, optional).
- `google-generativeai` is deprecated upstream in favor of `google-genai`; plan a migration window.
- `python-docx` + `reportlab` are heavy for serverless cold start; if `/api/export/chapter` is rarely used, consider lazy imports inside the route (cheap, zero-risk win for cold-start latency on every other route).
- Pin comment says verified 2026-04-21 — good practice; add `pip-compile`/`uv lock` to make it reproducible rather than comment-based.

### 3.17 `customs/*.json` (14 community files + `customs_db.json`) — LOW

- Propose a JSON Schema (`customs/schema.json`) + a pre-commit validation hook. Zero runtime impact, prevents an entire class of data bugs.

### 3.18 `scripts/` — LOW

- Operational one-offs (`migrate_customs_to_supabase.py`, `crawl_library_leaves.py`, etc.) and SQL setup files. Fine where they are. Add a `scripts/README.md` index noting which are one-time vs repeatable, and confirm none embed credentials (spot-check showed env-based access — good).

### 3.19 `docs/`, `README.md`, `DEVELOPER_NOTES.md` (×5 copies) — LOW

- Five separate `DEVELOPER_NOTES.md` files (root-adjacent dirs). Per-directory notes are defensible; add cross-links from `docs/MANIFEST.md` so they're discoverable. Archive stale audit docs per §1.

### 3.20 `.claude/` & `.agents/` — addressed in §2

- `.claude/settings.json` graphify hooks reference `graphify-out/graph.json`, which **does not exist in the workspace** — the hooks no-op safely (`[ -f … ]` guard), but either run `graphify` to regenerate the graph or remove the hooks/CLAUDE.md graphify rules to stop instructing agents to use a tool with no data.

---

## 4. Python performance / resource / async-safety summary

| Theme | Finding | Fix |
|---|---|---|
| Event-loop safety | All blocking calls in `asgi.py` correctly wrapped in `to_thread`; risk is future drift | Lint rule / convention doc: `requests` never imported in async modules; standardize on `httpx.AsyncClient` (shared, with timeouts) |
| Connection pooling | Multiple `requests.Session()` singletons (good); httpx clients possibly per-call | One shared `AsyncClient` per process with limits |
| Caching | ≥6 hand-rolled TTL dict caches | One bounded TTL/LRU utility in `backend/cache.py` |
| Thread pools | Per-request `ThreadPoolExecutor` in `app.py` | Module-level bounded executor |
| Rate limiting | Two implementations (Flask-Limiter + hand-rolled deque), both per-instance | Short-term: LRU eviction fix; long-term: Supabase/Upstash-backed counter if real enforcement matters |
| Serverless state | `DEVTOOLS_STATS`, caches, breakers reset per instance | Document as best-effort, or persist counters |
| Cold start | reportlab/python-docx imported eagerly | Lazy import in export route |

---

## 5. Zero-breakage strategy

Honesty first: **no refactor can be guaranteed regression-free with literal 100% certainty** — especially in a repo whose test suite was just moved to the trash folder. What follows is the strategy that gets asymptotically close, and nothing ships unless every gate passes.

1. **Characterization tests before any change.** Build a golden-master suite first: hit every one of the 48 routes (plus ASGI `/ask` and `/api/async/health`) against the current code with fixed inputs, record status + response shape (JSON schema, not exact text for AI routes). This snapshot is the contract.
2. **Move-only refactors.** Phase 1 extractions move functions verbatim — no logic edits in the same commit. A move commit either passes the golden suite identically or is reverted. Logic improvements are separate, later commits.
3. **Import shims.** Every function relocated out of `app.py` leaves a re-export behind (`from backend.auth import _verify_clerk_token`), so `asgi.py` and any untracked consumer keeps working. Shims are removed only in a final, separate phase after grep-verified zero usage.
4. **One change-set per PR/commit, ordered by risk:** (a) pure data moves (TOPIC_REFS → data file), (b) helper extraction with shims, (c) blueprint splits, (d) `/ask` pipeline unification, (e) vercel.json routing — each gated on the golden suite plus a Vercel preview deployment smoke test before production.
5. **Frontend extraction protocol:** one inline-script block per change, moved verbatim into a module, manual smoke checklist per feature (load page, exercise the feature, check console for errors), `window.ShelahModules` bridge retained until the end.
6. **Rollback:** every phase is a single revertable commit; vercel.json and service-worker changes additionally tested on preview URLs; SW change ships with a cache-version bump so a bad deploy self-heals on next deploy.
7. **Re-run `graphify update .`** after each structural phase (per project rules) once the graph is regenerated.

---

## 6. Proposed sequencing

| Phase | Work | Risk | Effort |
|---|---|---|---|
| 0 | Golden-master test suite; regenerate graphify graph | none | S |
| 1 | gitignore/docs cleanup; customs JSON schema; lazy imports; tokens.css | low | S |
| 2 | Extract backend helper modules from app.py (with shims); shared cache utility | low-med | M |
| **2.4** | **AI request timeout & retry resilience — no premature abort (§7.13)** | **low** | **S** |
| **2.5** | **AI source box fix — proper render + correct (AI-cited) sources (§7.1)** | **low** | **S** |
| **2.6** | **Full Flask route test coverage (§7.2)** | **none** | **M** |
| **2.7** | **Observability: logging, error tracking, cost monitoring (§7.3)** | **low** | **M** |
| **2.8** | **Complete documentation pass (§7.4)** | **none** | **M** |
| 3 | Blueprint split of routes; unify `/ask` pipeline; LRU rate-limit fix — **consolidated refactor (§7.6)** | med | L |
| 4 | index.html inline-JS extraction; style.css migration; skeleton states — folds into §7.6 | med | L |
| **4.5** | **Dark mode overhaul — token-based theming + 21st.dev patterns (§7.7, §7.9)** | **med** | **L** |
| **4.6** | **Motion overhaul — Framer Motion tooling-first workflow (§7.5, §7.8, §7.9)** | **med** | **M** |
| **4.7** | **Frontend platform fixes: Tailwind build, SRI, fonts, SEO, CSP hardening (§7.10)** | **med** | **M** |
| **4.8** | **Loading states overhaul — all loading animations/screens/AI loading, light + dark (§7.11)** | **low-med** | **M** |
| 5 | vercel.json static routing; SDK migrations (AsyncAnthropic, google-genai); shim removal | med | M |

---

## 7. Implementation Part 3 — expanded scope (added 2026-06-11)

Inserted after Phase 2 per request. Items 7.1–7.4 are low-risk and sequence immediately after the helper-module extraction; 7.5–7.8 fold into Phases 3–4.

### 7.1 AI source box fix (Phase 2.5) — grounded in confirmed bugs

Findings from `templates/index.html` (~line 10920) and `static/css/ai.css`:

1. **Event-handler destruction (the functional bug).** Source boxes are rendered with `sourcesDiv.innerHTML += sourceHtml` inside two sequential loops (Sefaria sources, then AI-cited sources). Every `innerHTML +=` re-parses the entire container, which **destroys the click listeners already attached** to the first batch's `.source-local-link` anchors — so "Open in Reader" links on primary sources go dead whenever "Additional References" render after them. Fix: build all boxes as detached nodes (or one accumulated HTML string), insert **once**, then wire all handlers **once** — or better, use a single delegated click listener on `sourcesDiv` so handlers can never be orphaned. Also O(n²) reparse cost disappears.
2. **Inconsistent escaping.** The first loop interpolates `${(src.ref || '').toUpperCase()}` raw into HTML while the second loop correctly uses `escapeHtml()`. Sefaria refs and English text lines can contain markup/quotes. Fix: `escapeHtml` everywhere; if Sefaria text segments intentionally carry HTML (footnote tags), sanitize with an allowlist instead of trusting raw API output.
3. **Duplicate conflicting styles.** `.ai-source-card` is defined in both `static/style.css:1375` and `static/css/ai.css:51` (plus dark variants in both, one with `!important`). Winner depends on stylesheet order. Fix: delete the `style.css` copy as part of the §3.12 migration; `ai.css` becomes the single owner.
4. **Staggered nth-child animation caps at 6** (`.ai-source-box:nth-child(1–6)`); boxes 7+ get no entrance animation and the section label `<p>` injected between groups shifts the nth-child counting, desynchronizing the stagger. Fix: stagger via a CSS custom property set per-box (`style="--i: n"`, `animation-delay: calc(var(--i) * 60ms)`) — immune to sibling insertion, unlimited count.
5. **Hover transform without reduced-motion guard** (`transform: translateY(-1px)` + `animate-fade-up`): wrap in `@media (prefers-reduced-motion: no-preference)` per ENGINEERING_RULES.
6. **Skeleton state.** Source area renders nothing until the answer resolves; add a layout-stable skeleton card row (header bar + two text lines) per the CLS ≈ 0 rule.

Verification: golden-master `/ask` fixture rendering both source groups; manual checklist — click every reader link before and after "Additional References" appear.

#### 7.1.A Status update (2026-06-14): items 1–6 are implemented; two **confirmed production regressions remain**

A re-audit of the live `populateAiModal()` (`templates/index.html:10791`) confirms items 1–6 above were already shipped — the source area now does a single atomic `sourcesDiv.innerHTML = _srcParts.join('')` write (line 10960), wires `a.source-local-link` handlers once afterward (10963), `escapeHtml`s every interpolation, staggers via `style="--i:n"` (10892/10942), and renders a skeleton row (10095). **However, the "right sources" half of the request is still broken, for two confirmed reasons:**

**Bug A — the production handler never sends `ai_cited_sources` (THE "wrong sources" root cause).**
The frontend reads the AI's *actually-cited* references from `data.ai_cited_sources` (`index.html:10906`) to render the "Additional References" group. The **Flask** `/ask` handler sets that key (`app.py:3335`, built from `structured_payload["sources"]` at `app.py:3321-3326`). The **ASGI** handler — which is what Vercel actually serves (`asgi.py` is the catch-all per `vercel.json`) — **omits `ai_cited_sources` from all three of its return payloads**: the success path (`asgi.py:402`), the strict-mode block (`asgi.py:307`), and the fallback path (`asgi.py:476`). Result in production: `data.ai_cited_sources` is always `undefined` → the "Additional References" group never renders, and the box shows **only** `data.sources` (the keyword-ranked *retrieved* primary sources from `_compact_ai_sources(primary_sources)`), which are not necessarily the sources the answer actually cites. This is the literal "not displaying the right sources" symptom.

**Bug B — `data.sources` is decoupled from the answer text.**
`rankAiSourcesForQuery()` (`index.html:10769`) re-ranks the *retrieved* primary sources by naive token overlap with the question+answer and shows the top 4. So even on the Flask path, the primary group can surface sources the AI didn't lean on while burying ones it did. The AI's own citation list (`structured.sources`) is the authoritative "right sources" set and must drive ordering/inclusion, with retrieved Sefaria text used to *enrich* those cited refs (fetch their `lines`), not to replace them.

**Exact fix — Claude Code prompt (paste verbatim):**

> Fix the AI source box so it shows the sources the answer actually cites, in production.
>
> 1. **`asgi.py` — restore `ai_cited_sources` parity with the Flask handler.** In the `/ask` ASGI handler, immediately after `display_sources = _compact_ai_sources(primary_sources)` in the **success** path (around line 399-401), build the cited list exactly as `app.py:3321-3326` does:
>    ```python
>    ai_cited = []
>    if isinstance(structured_payload, dict):
>        for s in (structured_payload.get("sources") or []):
>            s_str = str(s or "").strip()
>            if s_str:
>                ai_cited.append(s_str)
>    ```
>    Add `"ai_cited_sources": ai_cited,` to the returned dict (next to `"sources": display_sources,` at line 407). In the **fallback** path (return at line 476) add `"ai_cited_sources": [],` and in the **strict-block** path (return at line 307) add `"ai_cited_sources": [],` so the key is always present and the frontend never sees `undefined`.
> 2. **De-duplicate the logic.** `backend/ask_pipeline.py:326-331` already computes the identical `ai_cited` list. Extract a single helper `extract_ai_cited(structured_payload) -> list[str]` into `backend/helpers.py`, then call it from `app.py`, `asgi.py`, and `ask_pipeline.py` so the three paths can never drift again. Move-only refactor: behavior identical, verified by the golden-master `/ask` fixture.
> 3. **Make cited sources authoritative in the box (`templates/index.html`).** In `populateAiModal()` keep the existing single-write/single-wire structure, but render the **AI-cited** group *first* (it is the "right sources"), then render retrieved primary sources only for refs not already cited, de-duplicating with the existing `shownRefKeys` set in the other direction. Where a cited ref matches a retrieved primary source, attach that source's `lines` to the cited box instead of firing a separate `/api/text` fetch (saves a round-trip and guarantees the cited ref shows real text).
> 4. **Keep `rankAiSourcesForQuery` only as a tiebreaker** for the retrieved-but-not-cited remainder, not as the primary selector.
>
> Verification (must all pass before done): (a) add a golden-master `/ask` ASGI fixture whose `structured.sources` differs from the retrieved `primary_sources`, assert the JSON response contains `ai_cited_sources` equal to `structured.sources` on success and `[]` on strict/fallback; (b) snapshot-test that `app.py` and `asgi.py` `/ask` return byte-identical key sets; (c) manual: ask a question whose answer cites a source not in the retrieved set and confirm it appears in "Additional References" with text, in both light and dark themes, and that every "Open in Reader ↗" link works after the async text fetches resolve.

Cross-reference: this is the §7.1 work referenced by the Phase 2.5 row; the engineering invariant that prevents recurrence ("`/ask` response schema parity across transports; AI-cited sources must always reach the client") is codified in `.agents/ENGINEERING_RULES.md` → *AI request resilience & source integrity*.

### 7.2 Full test coverage on all Flask routes (Phase 2.6)

Extends the Phase 0 golden-master suite into a maintained pytest package:

- `tests/conftest.py` — Flask `test_client` + FastAPI `TestClient` (httpx) fixtures; env-var fixture forcing test mode; `responses`/`respx` mocks for Sefaria, Hebcal, Anthropic, Gemini, Supabase, translation APIs so the suite runs **offline and deterministic**.
- **One test module per blueprint** (mirrors the §3.1 split): `test_routes_library.py`, `test_routes_prayers.py`, `test_routes_community.py`, `test_routes_calendar.py`, `test_routes_user.py`, `test_routes_devtools.py`, `test_routes_core.py` (`/`, `/terms`, `/privacy`, manifest, favicon, service-worker), `test_ask.py` (both Flask and ASGI variants).
- For every one of the 48 routes: happy path (status + JSON-schema/shape assertion), auth-required path (401 without bearer where `require_clerk_auth` applies), malformed-input path (400s; coordinate bounds, bad refs, oversized payloads), and upstream-failure path (mocked 5xx → graceful fallback verified, circuit-breaker behavior in `health_check.py`).
- `/ask`-specific: strict-mode block, prayer-keyword early return, fallback ladder (AI failure → `get_halakhic_sources`), sanitization flag, rate-limit 429 (deque limiter unit-tested directly with monkeypatched clock).
- Unit tiers for pure helpers as they're extracted in Phase 2 (`answers.py`, `rag.py`, `translate.py` scoring/normalization functions) and DST-boundary zmanim tests (§3.7).
- Coverage gate: `pytest-cov` with `--cov-fail-under=85` on `backend/` and route modules; wire into pre-commit/CI. (100% line coverage on a 5,000-line legacy module is a vanity target; 85% enforced + 100% **route** coverage is the honest, useful contract.)

### 7.3 Observability end-to-end: logging, error tracking, cost monitoring (Phase 2.7)

**Structured logging**
- Extend `backend/logging_setup.py`: JSON log formatter (Vercel log drains parse JSON), per-request `request_id` (accept inbound `X-Request-ID` or generate UUID) propagated via `contextvars` so Flask threads *and* asyncio tasks share it; log `route`, `latency_ms`, `status`, `user_id` (hashed), `upstream_calls`.
- FastAPI middleware + Flask `before/after_request` hooks emit one access-log line per request; slow-request warning threshold (e.g. >3s).
- Replace silent `except Exception: pass/None` sites (§3.2) with `logger.debug`/`logger.warning` minimum.

**Error tracking**
- Adopt Sentry (`sentry-sdk` has first-class Flask + FastAPI + asyncio integrations and a free tier) initialized once in `logging_setup.py`; `_capture_backend_error` becomes a thin wrapper that both logs and forwards to Sentry with context — call sites unchanged (zero-breakage). Frontend: `installGlobalErrorBoundary()` in `reader-ui.js` already posts to `/api/client-errors`; forward those to Sentry too, tagged `source: browser`.
- Release tagging from git SHA so regressions map to deploys.

**Cost monitoring**
- New `backend/cost_meter.py`: every model call (Claude sync/async, Gemini, translation APIs) records provider, model, input/output tokens (from API response `usage` fields), computed USD cost from a price table, route, and request_id.
- Sink: Supabase table `ai_usage_log` (insert via `to_thread`, fire-and-forget with local buffer on failure) — survives serverless instance churn, unlike `DEVTOOLS_STATS`.
- Surface: extend `/api/devtools/reliability` with per-day/per-route cost rollups; alert hook (log-based) when daily spend exceeds an env-configured budget.
- Token-cost guardrails: assert `max_tokens` caps on every call site; log prompt sizes so prompt-bloat regressions are visible.

### 7.4 Complete documentation pass — one shot (Phase 2.8)

Single PR regenerating all living docs against post-Phase-2 reality:

- `README.md` — rewrite: what Sh'elah is, architecture diagram (Flask+FastAPI hybrid, Vercel, Supabase, Sefaria/Hebcal/AI upstreams), quick start, env var table (every `os.environ` read in the repo, enumerated), test instructions.
- `docs/SERVICE_ARCHITECTURE.md` — update to reflect extracted `backend/` modules and the ask-pipeline; add sequence diagram for `/ask`.
- `docs/API.md` (new) — all 48+ routes: method, path, auth, params, response schema, error codes. Generated as a first pass from the route table + golden-master schemas, then hand-annotated. FastAPI side gets `/docs` (OpenAPI) for free — link it.
- `docs/OBSERVABILITY.md` (new) — logging fields, Sentry setup, cost-meter schema, devtools endpoints.
- `docs/FRONTEND.md` (new) — module map of `static/js/`, state store contract, theming tokens, motion conventions (§7.5).
- Module docstrings on every `backend/*.py` (most already have good headers — fill gaps), and the five `DEVELOPER_NOTES.md` files cross-linked from `docs/MANIFEST.md`.
- Archive stale audit docs to `docs/archive/` (per §1) in the same PR so the docs tree ends clean.

### 7.5 Framer Motion adoption — honest scoping, then full motion replacement

**Reality check (important):** Framer Motion is a **React library**, and this codebase currently contains **zero React** — the frontend is vanilla ES modules + inline scripts + CSS animations. "Replace all motions with Framer Motion" therefore needs one of two paths, and the plan includes both:

- **Path A (recommended, immediate): `motion` (motion.dev)** — the successor library from the same author, with a framework-agnostic vanilla API (`animate()`, `scroll()`, springs, exit animations) that delivers the identical spring-physics feel without introducing React. ~5kb, loadable as an ES module. All ~87 CSS `animation`/`transition` rules get audited; meaningful movement (source-box stagger, panel slides, modal enter/exit, sidebar, calendar transitions) migrates to `motion` springs driven from JS; trivial hover color fades stay as CSS (correct per ENGINEERING_RULES — tweens for color/opacity).
- **Path B (as React surfaces appear): full Framer Motion** — any new or rebuilt UI islands (the AI ask panel and reader are the natural first candidates if/when they're rebuilt as React components) use Framer Motion proper under the `.agents/ENGINEERING_RULES.md` mandates: `AnimatePresence` for mount/unmount, `layoutId` shared transitions, `useMotionValue`/`useTransform`, hoisted variants, `React.memo`, `useReducedMotion`.

Concrete replacement inventory (Path A, executed with §7.6):

| Current | Replacement |
|---|---|
| `.animate-fade-up` CSS keyframe + nth-child stagger (ai.css) | `motion` `animate()` with per-element stagger + spring |
| Hover `translateY(-1px)` transforms | spring-driven hover via `motion` press/hover gestures (or keep CSS, reduced-motion-guarded) |
| Modal/panel show/hide via `.hidden` class toggles | `motion` enter/exit (vanilla equivalent of `AnimatePresence`) so elements animate out before removal |
| Sidebar/drawer slide transitions | spring physics, `transform`-only |
| Calendar month swap | crossfade + spring slide |

Global rules enforced in one shared `static/js/motion.js` helper: respect `prefers-reduced-motion` centrally, animate `transform`/`opacity` only, no layout-property animation.

### 7.6 The "one-shot" consolidated refactor — scoped honestly

Request acknowledged: do the remaining refactor in one shot. The plan consolidates Phases 3+4 (blueprint split, `/ask` unification, index.html inline-JS extraction, style.css migration, skeletons) plus §7.5 Path A and §7.7 into **one continuous work track executed as a single milestone** — but internally it remains a sequence of individually-revertable commits gated on the (by then complete) test suite from §7.2. A literal single-commit big-bang rewrite of a 28,000-line codebase with live users would be malpractice, and the zero-breakage strategy in §5 is incompatible with it; this milestone structure gives you "everything lands together" without betting the app on an unrevertable diff. Exit criteria for the milestone: all routes green, coverage gate passing, Lighthouse a11y ≥ 95, CLS ≈ 0 on the three async-heavy views, preview deployment soak before production cutover.

### 7.7 Full dark mode overhaul (UI/UX Pro Max rules)

Current state, measured: **574 scattered `body.theme-dark` selector overrides** across 9 files (452 in `style.css` alone), many with `!important`; **zero `prefers-color-scheme` support** in any stylesheet (only 3 references in index.html scripts); duplicated dark variants for the same components in competing files (§7.1.3).

Overhaul design:

1. **Token layer first** (extends §3.12 `tokens.css`): define semantic CSS custom properties — `--surface-0/1/2`, `--ink-primary/secondary/muted`, `--accent`, `--border`, `--shadow-color`, state colors — with light values on `:root` and dark values on `[data-theme="dark"]`. Components reference tokens only; **the 574 overrides collapse into one theme block**.
2. **System preference + manual override:** default to `prefers-color-scheme` via `@media`-scoped token redefinition, overridable by the existing user toggle writing `data-theme` (keep `body.theme-dark` as a temporary alias class during migration so untouched selectors keep working — zero-breakage shim, removed at the end).
3. **No-flash theming:** inline `<head>` script sets `data-theme` from localStorage/system before first paint (verify existing toggle's behavior; FOUC is the classic bug here). Add `color-scheme: light dark` and `<meta name="theme-color">` per theme for correct scrollbars/form controls/PWA chrome.
4. **AA contrast audit, both themes:** scripted check (axe-core or pa11y in CI) of every token pairing against WCAG 2.1 AA (≥4.5:1 text, ≥3:1 UI). Dark theme commonly fails on muted text and borders — fix at the token level, never per-component.
5. **Dark-mode-specific polish:** elevation via lighter surfaces (not heavier shadows); desaturate accent colors to avoid vibration on dark; check the Hebrew serif font (`SILEOT`) rendering weight on dark backgrounds; images/icons get `color-scheme`-aware treatment; skeleton shimmer colors per theme.
6. **Migration order:** tokens → topbar/sidebar → reader → AI panel → calendar → settings/legal pages, one commit each, visual-regression screenshots (Playwright) per step.

### 7.8 React-based motion skills — full overhaul using Framer Motion

The `.agents/ENGINEERING_RULES.md` Framer Motion section (written in Part 1) is the policy; this item makes it executable:

- **Skill upgrade:** expand `ENGINEERING_RULES.md` §Framer Motion into a full reference with code patterns — variant architecture (parent `staggerChildren`, hoisted variant objects), `AnimatePresence` `mode="wait"` vs `"popLayout"` decision table, `layoutId` morph recipes (card → modal), gesture springs (`whileHover`/`whileTap` with spring transitions), scroll-linked `useScroll`+`useTransform`, and the performance-guard checklist as a PR review gate.
- **Applicability:** binds to Path B of §7.5 — enforced automatically on the first React surface introduced. Until then it governs any React prototyping and keeps the vanilla `motion` implementation API-shaped so a future React migration maps 1:1 (`animate()` → `<motion.div>`, vanilla exit handling → `AnimatePresence`, manual springs → variants).

### 7.9 21st.dev rule sets & libraries + Framer Motion tooling (applies to all stages after Stage 3)

Binding directive for every UI/dark-mode/motion change from Phase 3 onward (codified in `.agents/ENGINEERING_RULES.md` §Component & Motion Tooling):

**21st.dev for UI changes (dark mode included)**
- 21st.dev is a registry of production-grade React/Tailwind components (shadcn-style) plus the **Magic MCP** server for AI-driven component generation. The site already loads Tailwind + DaisyUI, so 21st.dev's Tailwind class patterns transfer directly even before any React exists.
- Rules adopted: (a) before hand-building any non-trivial UI element (card, modal, command palette, settings panel, toast, skeleton), check 21st.dev for an established pattern and adapt its markup/Tailwind classes — dark variants included, since 21st.dev components ship `dark:` styling that maps onto our `[data-theme="dark"]` token layer; (b) dark-mode component work in §7.7 uses 21st.dev dark-theme component references as the visual QA baseline; (c) when React islands land (§7.5 Path B), 21st.dev components are installed directly (shadcn-compatible) rather than re-implemented; (d) the Magic MCP is wired into the agent toolchain so component generation goes through tool calls, not freehand markup.
- Honest caveat: 21st.dev components are React-first. Until React surfaces exist, usage is pattern/markup adaptation; the plan does not pretend a vanilla page can `npm install` a React component.

**Framer Motion via proper tool calls (all motion changes)**
- All motion work in Phases 4.6+ is executed through a tooling-first workflow, not hand-typed animation code: the agent toolchain's UI/UX skill (`ui-ux-pro-max`, which carries Framer Motion stack guidance) is invoked for every motion task; on React surfaces, Framer Motion APIs are used exclusively and verbatim per `.agents/ENGINEERING_RULES.md` — `<motion.*>` components, `AnimatePresence`, `layoutId`, `useMotionValue`/`useTransform`, `useReducedMotion` — never raw CSS keyframes or rAF loops.
- On current vanilla surfaces, the `motion` (motion.dev) API is the mandated call surface (`animate()`, `spring()`, `stagger()`, `inView()`), chosen specifically because each call maps 1:1 onto a Framer Motion equivalent for the React migration.
- Enforcement: PR checklist item — "all new/changed animation goes through motion/Framer Motion calls; zero new `@keyframes`, zero new `transition` on transform properties"; CI grep guard for `@keyframes` additions outside `tokens.css`.

### 7.10 Frontend platform fixes — site-wide findings from the deep scan (Phase 4.7)

A second full pass over `templates/index.html` head, CSP, assets, and delivery surfaced these (all confirmed, with line evidence):

1. **Tailwind CDN runtime compiler in production (CRITICAL).** `<script src="https://cdn.tailwindcss.com">` ships the entire JIT compiler (~300KB+) to every visitor, compiles classes in the browser on every load, and is the reason the CSP must allow `'unsafe-eval'`. Tailwind's own docs forbid it for production. Fix: a tiny build step (Tailwind CLI standalone binary — no Node project required) generating one purged `tailwind.css` at deploy; DaisyUI moves into that build (`@plugin`). Removes the script, the eval permission, and hundreds of ms of main-thread work. This is the single biggest frontend performance win available.
2. **CSP hardening.** Current policy (app.py:2745) allows `script-src 'unsafe-inline' 'unsafe-eval'`. After (1) kills the eval requirement and §3.10 extracts inline scripts into `static/js/` modules, tighten to nonce-based `script-src 'self' 'nonce-…'` + pinned CDNs; drop the obsolete `X-XSS-Protection` header (deprecated; can introduce vulnerabilities in old browsers). Add `Permissions-Policy` and `Strict-Transport-Security` (Vercel sets HSTS, verify).
3. **No SRI on CDN dependencies.** `marked` (unpinned version!), `dompurify@3` (major-only pin), DaisyUI 4 — all from jsdelivr with no `integrity` hashes. A CDN compromise is a full XSS. Fix: pin exact versions + SRI hashes, or self-host all three (preferred once a build step exists from (1)).
4. **Font loading.** No `<link rel="preconnect">` to `fonts.googleapis.com`/`fonts.gstatic.com` (adds ~100–300ms to first text paint); local `SILEOT.woff` should be converted to woff2 (~30% smaller) with a `@font-face` `font-display: swap` and a `preload` hint since it renders all Hebrew text above the fold.
5. **Zero SEO/social metadata.** No `<meta name="description">`, no Open Graph or Twitter card tags, no canonical URL, no JSON-LD. For a public Torah encyclopedia this is significant discoverability left on the table. Fix: full head metadata block + per-route titles when blueprints render legal pages; generate `og:image` from the favicon artwork.
6. **Theme FOUC confirmed.** `<html lang="en" data-theme="light">` is hardcoded and `theme-color` meta is single-valued; dark-mode users get a light flash every load. Fixed by §7.7.3 (pre-paint head script + dual `theme-color` metas with `media` attributes).
7. **Manual cache-busting drift.** Stylesheets carry hand-edited `?v=20260512-v1` strings — `ai.css` is already on a different date than its siblings, proving the mechanism doesn't scale. Fix: template variable injecting one build hash everywhere (also feeds the service-worker cache name from §3.13).
8. **`#quick-settings-dark-fixes` inline style block** in the head is a patch-on-patch (dark-mode `!important` fixes for the settings panel) — exactly the class of debt §7.7's token layer eliminates; fold it in and delete.
9. **8 render-blocking stylesheets** load sequentially in the head; after the §3.12 consolidation, emit `tokens.css` + one built `tailwind.css` + one app bundle, and `preload` the two above-the-fold ones.
10. **Hebrew/i18n correctness:** `<html lang="en">` is static even in Hebrew mode — switch `lang` and `dir="rtl"` dynamically with the language toggle (screen readers and font selection depend on it). Audit RTL layout of source boxes and reader in Hebrew mode.

### 7.11 Loading states overhaul — all loading animations, screens, and AI loading, light + dark (Phase 4.8)

Full audit of every loading surface, with confirmed current-state findings:

**Findings**
1. **AI loading animation has no dark mode at all.** The menorah-inspired spinner block (`index.html` ~9135) is built entirely from hardcoded inline hex values — `#002147` rings, `#b8a07a` accent, `text-[#002147]`, pastel source tags (`bg-[#e8f4f5]`, `bg-[#f3f0ff]`, `bg-[#fef9e6]`) — with inline `style="animation: …"` attributes. In dark mode it renders as a light-theme artifact: near-invisible navy rings on dark background, glaring pastel chips.
2. **Phase-text cycler leaks timers.** `aiLoadingPhases.forEach` schedules `setTimeout`s that are never cancelled when the answer arrives or the request fails — late timers fire into a DOM that may have been replaced (benign today only by luck of element-id checks).
3. **`#loadingOverlay` (full-screen) is patch-styled.** Toggled via `.hidden` in 8+ call sites; dark mode bolted on through `body.theme-dark #loadingOverlay` overrides and an `!important` `warmPulse` animation in `sidebar.css` (a sidebar file styling a global overlay — wrong owner).
4. **Seven+ regions still use bare "Loading..." italic text** (library index ×3, prayer books, sidebar tree, reader panels) — no skeleton, guaranteed layout shift, violating the CLS ≈ 0 rule.
5. **Skeletons exist only in two places** (AI source `ai-src-skeleton` — good, has dark + reduced-motion variants; reader `reader-loading-shimmer`). Calendar grid, zmanim panel, prayer lists, library tree, search suggestions have none.
6. **Six+ competing animation systems:** `shelah-shimmer`, reader `shimmer`, `shelah-spin`, `shelah-dot-bounce`, `warmPulse`, Tailwind `animate-pulse` — duplicated timing curves, no shared tokens, most without `prefers-reduced-motion` guards.
7. **Accessibility gaps:** loading regions lack `role="status"`/`aria-busy`; the cycling phase text has no `aria-live="polite"`; spinner `aria-hidden` is correct but the only correct part.

**Revamp design (executes with §7.6/§7.7 milestone; obeys §7.9 tooling rules)**
1. **One loading design system, token-driven.** New `static/css/loading.css` (or section of tokens layer) defining skeleton surface/highlight, spinner stroke, overlay scrim, and pulse-text colors as semantic tokens with light values on `:root` and dark on `[data-theme="dark"]` — every loading visual references tokens only. Deletes findings 1 and 3 at the root; the `ai-src-skeleton` dark block collapses into it.
2. **Component inventory → states.** Per async region, the right primitive: *skeleton* (content-shaped: library tree rows, calendar grid cells, prayer list rows, reader paragraphs, AI sources — already done), *spinner* (indeterminate short waits: search icon), *staged progress* (AI answer: keep the menorah spinner + phase text + source chips concept — it's good UX — rebuilt with token colors, classes instead of inline styles/hex, dark variants, and timer cleanup via a single `AbortController`-style cancel on resolve/reject).
3. **All "Loading..." text placeholders replaced with skeletons** matching final content dimensions (CLS ≈ 0), shimmer via one shared keyframe driven by `motion`/`stagger()` per §7.9 (CI guard exemption: the single shared shimmer keyframe lives in the loading/tokens layer).
4. **Overlay rework:** `#loadingOverlay` becomes a token-styled component with fade-in only after a 150ms delay (no flash on fast loads), fade-out before unhide of content (vanilla AnimatePresence-equivalent per §7.9), scrim contrast verified AA in both themes, styles moved out of `sidebar.css` to the loading layer, `!important`s removed.
5. **Motion rules applied:** every loading animation honors `prefers-reduced-motion` (static skeleton, no shimmer/bounce; finding 6's unguarded keyframes all gated); animations on `transform`/`opacity` only.
6. **Accessibility:** every loading region gets `role="status"` + `aria-busy="true"` while pending; AI phase text gets `aria-live="polite"`; skeletons `aria-hidden="true"`; focus is not stolen by overlay show/hide.
7. **21st.dev baseline (§7.9):** skeleton and spinner components are visually QA'd against 21st.dev's skeleton/loader patterns in both themes before sign-off.
8. **Verification:** Playwright visual-regression shots of every loading state in light and dark; throttled-network manual pass; axe-core check on busy states; timer-leak test asserting zero pending timeouts after answer resolution.

### 7.12 Additional improvements found during this pass

- **`escapeHtml` consistency audit** across all ~9,500 lines of inline JS — the §7.1.2 finding is unlikely to be the only raw interpolation; grep-driven audit of every `innerHTML` assignment (there are many) for unescaped user/API data. Security-relevant.
- **Event-delegation sweep:** the `innerHTML +=` handler-loss pattern (§7.1.1) likely recurs in reader sources (`#readerSources`), calendar, and prayer lists — same delegated-listener fix everywhere.
- **`aria-live="polite"` on the AI answer container** so screen readers announce answers; `role="status"` on skeletons; keyboard focus management when panels open (focus trap in modals).
- **Service-worker cache versioning** (§3.13) becomes urgent once CSS/JS restructuring ships — build-hash cache names in the same milestone as §7.6.
- **`requirements-dev.txt`** (pytest, pytest-cov, respx, responses, ruff, pre-commit pins) separated from runtime deps to keep Vercel cold start lean.
- **CI pipeline** (GitHub Actions or Vercel checks): ruff + pytest + coverage gate + axe-core a11y check + Playwright visual regression on the six themed views — the enforcement backbone for everything above.

### 7.13 AI request timeout & retry resilience — "no premature abort after a couple of tries" (Phase 2.4)

**Confirmed root cause (measured 2026-06-14).** Users see the AI "time out after only a couple of tries." Three independent facts combine to produce this:

1. **The browser hard-aborts `/ask` at 10 s, single-shot, no retry.** `templates/index.html:9109-9110`:
   ```js
   const abortCtrl = new AbortController();
   const abortTimer = setTimeout(() => abortCtrl.abort(), 10000);
   ```
   On abort the catch block (`9150-9152`) shows *"Response timed out. Please try again."* There is **no automatic retry** — the user must manually re-ask, and each manual attempt restarts the whole pipeline cold.
2. **The server pipeline routinely needs more than 10 s.** The ASGI handler fans out six tasks with `asyncio.gather` (`asgi.py:271`: primary Sefaria sources, Halachipedia, wiki, community knowledge, user memory, tool context) **and then** makes the model call. Cold-start + upstream latency on Sefaria/Hebcal alone can approach the 10 s budget before the LLM is even invoked.
3. **The model layer itself can burn the whole budget in retries.** `_generate_gemini_content_with_retry` (`backend/claude.py:284-289`) retries up to **5 times** with `wait_random_exponential(min=1, max=4)` on `ResourceExhausted` — that backoff ladder alone can exceed 10 s, so the browser aborts *mid-retry*: literally "timed out after a couple of tries." Meanwhile **`AI_MODEL_TIMEOUT_SECONDS` (default 8, `backend/claude.py:81`) is dead config** — it is never passed to `AsyncAnthropic` (created with no `timeout=`/`max_retries=` at `claude.py:207`) nor to the Gemini call, so the documented timeout knob does nothing.

**Design — coordinate three budgets so the client never gives up before the server has had a fair, bounded chance:**

| Layer | Today | Target |
|---|---|---|
| Per-model call timeout | unbounded (SDK default ~600 s); Gemini none | wire `AI_MODEL_TIMEOUT_SECONDS` into both providers; default raise to ~25 s |
| Provider retry budget | Gemini 5× exp backoff (≤ ~20 s); Claude SDK default 2× | cap to fit under the total server budget; fail fast to fallback ladder |
| Total server request budget | implicit | explicit `AI_TOTAL_BUDGET_SECONDS` (e.g. 40 s); wrap synthesis in `asyncio.wait_for`, on timeout return the `get_halakhic_sources` fallback payload, not a 500 |
| Client abort | 10 s, 0 retries | ceiling ≥ server budget + headroom (e.g. 50 s) **and** 2 automatic retries with backoff on `AbortError`/network/`5xx`/`502`, with staged "still working" messaging |

**Exact fix — Claude Code prompt (paste verbatim):**

> Make the AI request resilient so it is not aborted after a couple of tries. Three coordinated changes; keep all existing fallback behavior.
>
> 1. **Frontend (`templates/index.html`, the `/ask` flow at ~9107-9160).** Replace the single 10 s abort with a bounded retry wrapper:
>    - Read the ceiling from a constant `const AI_REQUEST_TIMEOUT_MS = 50000;` and `const AI_MAX_ATTEMPTS = 3;`.
>    - Wrap the existing `fetch('/ask', …)` in an `async function askWithRetry(body, headers)` that loops up to `AI_MAX_ATTEMPTS`. Each attempt gets its own `AbortController` + `setTimeout(…, AI_REQUEST_TIMEOUT_MS)` cleared in a `finally`. Retry only on `AbortError`, `TypeError` (network), or HTTP `502/503/504`; do **not** retry on `4xx` or a normal `200`. Back off `1200ms * attempt` between tries.
>    - Drive the loading UI: on attempt ≥ 2 append a phase string `t('Still working — retrying…', 'עדיין עובד — מנסה שוב…')` to the `_phaseTimers` cycle so the user sees progress instead of a dead spinner. Continue to clear all timers in the existing `finally` (line 9157) — no orphaned timers (ENGINEERING_RULES loading-state rule 5).
>    - Only after all attempts fail show the timeout message; log `logReliability('ai-network-error', { timeout, attempts })`.
> 2. **Model layer (`backend/claude.py`).** Actually apply the timeout knob:
>    - At `_get_async_client()` (line 197-207) pass `timeout=MODEL_REQUEST_TIMEOUT_SECONDS` and `max_retries=2` to `anthropic.AsyncAnthropic(...)`.
>    - Raise the default: `MODEL_REQUEST_TIMEOUT_SECONDS = _int_env("AI_MODEL_TIMEOUT_SECONDS", 25)`.
>    - Pass a request timeout into the Gemini call config and reduce `stop_after_attempt(5)` to `stop_after_attempt(3)` so the Gemini backoff ladder cannot exceed the server budget.
> 3. **Server total budget (`asgi.py`, `/ask`).** Add `AI_TOTAL_BUDGET_SECONDS = int(os.environ.get("AI_TOTAL_BUDGET_SECONDS", "40"))`. Wrap the `claude.ask_ai_async(...)` synthesis call (line 333) in `await asyncio.wait_for(..., AI_TOTAL_BUDGET_SECONDS)`; on `asyncio.TimeoutError` route into the **existing** `except` fallback block (the `get_halakhic_sources` path at 440-504) rather than raising — the user gets discovered references, never a hung request or a 500. Mirror the same wrapper in the Flask `/ask` (`app.py`) for parity.
>
> Verification (all must pass): (a) unit-test `askWithRetry` with a mocked `fetch` that aborts twice then succeeds — assert 3 attempts, success surfaced, zero pending timers afterward; (b) backend test: monkeypatch `ask_ai_async` to sleep past `AI_TOTAL_BUDGET_SECONDS`, assert the response is the graceful fallback payload (200 with `meta.fallback=true`), not a 500; (c) assert `AsyncAnthropic` is constructed with the configured `timeout`/`max_retries`; (d) manual throttled-network run confirms the spinner shows the retry phase text and resolves rather than dying at 10 s.

**Honest caveat:** Vercel serverless functions have their own platform execution ceiling — set `AI_TOTAL_BUDGET_SECONDS` and the `functions.maxDuration` in `vercel.json` (§3.15) consistently below that ceiling, and keep the client ceiling above the server budget, or the platform will kill the function before either timeout fires. Tune the three numbers together; they are intentionally env-configurable so production can be adjusted without a code change.

### 7.14 Invariants to prevent recurrence (from the §7.1.A / §7.13 audit)

- **`/ask` transport parity is now a tested invariant** (see §7.1.A bug A and §7.13 step 3): the Flask and ASGI handlers must return the same JSON key set on every path (success, strict, fallback). Add a single shared response-builder so the two transports cannot drift in keys, timeouts, or fallback semantics — this directly prevents both the missing-`ai_cited_sources` and the uncoordinated-timeout classes of bug from recurring.
- **Dead-config lint:** `AI_MODEL_TIMEOUT_SECONDS` was defined-but-unused for months. Add a tiny test asserting every `os.environ`/`_int_env` config constant is referenced at least once outside its definition, so silently-dead knobs are caught.

---

Awaiting your review before any code modification beyond the workspace organization and directive updates you authorized.
