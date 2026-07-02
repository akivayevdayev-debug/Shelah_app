# Claude Code Prompts — one per plan.md section

Copy-paste each prompt into Claude Code **one at a time, in order within each track**. Every prompt assumes Claude Code is run from the repo root with `plan.md` present. Backend prompts (1–4) are strictly sequential; other tracks can run independently.

Universal rules baked into every prompt: golden-master tests before moves, move-verbatim not rewrite, back-compat shims, no new import cycles, reconcile §2 duplicates, `pytest -q` green before done, `graphify update .` after code changes, stop when the prompt's scope is complete — do not continue into the next section.

---

## Prompt 1 — §4 Phase 1: extract text/formatting → `backend/utils/text_engine.py`

```
Read plan.md §2, §3, §4 PHASE 1, §5, and §6 in full before writing any code. Execute PHASE 1 exactly as specified.

Scope: move from app.py into a new backend/utils/text_engine.py the constants HEBREW_DIACRITICS_RE, CLOCK_TIME_LATEX_RE, HALAKHIC_VERDICT_RE, UI_SECTION_KEYS and the functions _strip_model_web_warning_prefix, _normalize_ai_answer, _bold_halakhic_verdicts, _collapse_markdown_spacing, _format_ui_answer, plus any private helpers in their call-closure (verify with `graphify query`/`graphify path`).

Procedure (strict order):
1. Read-only: build the call-closure; grep each target body for Flask globals (g, session, request, current_app) — any function using them is out of scope, flag it and skip.
2. Write tests/test_text_engine.py FIRST as golden-master characterization tests against current app.py behavior (halakhic verdict line, clock-time LaTeX artifact, multi-blank-line markdown, web-warning prefix, Hebrew diacritics string, and edge cases: string cut mid-**bold**, mid-<tag> — no dangling markdown/HTML ever).
3. Create backend/utils/__init__.py and backend/utils/text_engine.py; COPY the symbols verbatim (no rewrites); text_engine must import nothing from app.
4. Import the moved names back into app.py as back-compat shims so blueprints importing from app keep working; only then delete the originals from app.py.
5. Reconcile the HEBREW_DIACRITICS_RE duplicate: canonical copy in text_engine.py, helpers.py re-imports it, app.py copy deleted. First verify semantic equivalence of the two regexes with a small check.
6. Verify: python -m pytest -q green; python -c "import app, asgi, backend.utils.text_engine" clean; run graphify update . and confirm no new import cycle.

Do NOT touch retrieval/search functions (that is Phase 2). Stop when Phase 1 exit criteria pass and report.
```

## Prompt 2 — §4 Phase 2: extract retrieval/corpus matching → `backend/utils/search_provider.py`

```
Read plan.md §2, §3, §4 PHASE 2, §5, §6. Phase 1 (text_engine) is already merged. Execute PHASE 2 exactly as specified.

Scope: move from app.py into new backend/utils/search_provider.py: HEBREW_WORD_GLOSSARY, HALAKHIC_CORPUS_ALIASES, QUERY_STOPWORDS, HEBREW_PREFIXES; _strip_common_hebrew_prefixes, _expand_hebrew_keyword_forms, _extract_query_keywords; _query_search_wrapper, _collect_global_sefaria_sources, _iter_local_json_matches, _find_local_custom_matches, get_halakhic_sources, plus their call-closure helpers (_match_corpus, _extract_hit_snippet, _is_sefaria_hit_relevant, _build_discovery_queries, _collect_external_global_sources, _dedupe_ordered_text, etc. — determine the exact set via graphify closure).

Constraints:
- search_provider.py imports backend.* modules (sefaria, sefaria_library, search, data_service) directly, NEVER app. If a function uses _THREAD_POOL, take it as a parameter or import lazily inside the function.
- Reconcile the HEBREW_WORD_GLOSSARY duplicate (app.py L174 vs helpers.py L139) and the DIVERGED _translate_text_google/_translate_text_mymemory pair per plan.md §2: diff, pick the test-anchored correct behavior, keep ONE canonical implementation, delete both old copies, repoint all callers.
- Golden-master tests first: tests/test_search_provider.py covering Hebrew prefix stripping, stopword removal, glossary expansion, local-JSON-match correctness against a fixture corpus, and get_halakhic_sources happy-path + empty-result fallback shape — all offline via conftest mock_outbound_http.
- Copy verbatim → shims in app.py (blueprints and ask_pipeline.py's flask_app_module.get_halakhic_sources keep working) → delete originals.

Verify: full pytest green (esp. test_ask.py, test_ask_pipeline_smoke.py); no import edge search_provider→app after graphify update .; python -c "import app, asgi, backend.utils.search_provider" clean. Stop and report. Do not start Phase 3.
```

## Prompt 3 — §4 Phase 3: circuit-breaker hardening on `search_provider` network calls

```
Read plan.md §3, §4 PHASE 3, §5, §6. Phases 1–2 are merged. Execute PHASE 3 exactly as specified.

Reuse the EXISTING breaker in backend/health_check.py (APIHealth, FAIL_THRESHOLD=3, RECOVERY_INTERVAL=120) — do not build a new mechanism. Wrap all external calls now in backend/utils/search_provider.py (Sefaria via sefaria_library, _collect_external_global_sources, and the reconciled _translate_text_google/_translate_text_mymemory) with per-service keys: 'sefaria', 'hebcal', 'translate_google', 'translate_mymemory', 'web' (extend the service registry additively if needed).

Pattern per call: check health.is_healthy(service) before → skip to fallback if open; record_success on success; on timeout/connection error record_failure then FAIL OPEN to local corpus (_find_local_custom_matches / _iter_local_json_matches).

Defensive rules: every network call gets an explicit timeout= (audit calls relying on session defaults); catch narrowly (requests.RequestException, httpx.HTTPError, TimeoutError) — never bare except Exception; log warnings via logging_setup. get_halakhic_sources must return a well-formed local-corpus payload even when ALL providers are circuit-open — never raise to the route.

Tests: extend test_health_check.py patterns — N consecutive failures opens the circuit; get_halakhic_sources transparently serves local corpus while open; half-open re-probe after RECOVERY_INTERVAL; use the suite's monkeypatched-clock approach. Full pytest green, graphify update ., stop and report. Do not start Phase 4.
```

## Prompt 4 — §4 Phase 4: streamline `app.py` & finalize wiring

```
Read plan.md §4 PHASE 4, §5, §6. Phases 1–3 are merged. Execute PHASE 4.

1. Confirm every migrated block (formatting regex/constants, glossary tables, formatting fns, retrieval fns) is DELETED from app.py; only re-export shims remain, each commented as "compatibility shim — remove in cleanup ticket".
2. Verify app.py retains exactly: Flask app construction, env bootstrapping, global middleware + security headers, _THREAD_POOL, user-scoped Supabase auth glue (reconcile any auth duplication with backend/auth.py the same reconcile-then-consolidate way as plan.md §2), blueprint registration, and the thin /ask route delegating to ask_pipeline.
3. Re-point internal callers to canonical import paths where low-risk; keep shims only where removal touches many blueprint files (defer those, list them in the report).
4. Final: full pytest green; graphify update . and confirm zero cycles + reduced app.py density; report the new app.py line count (target: meaningfully below 3,560).

Stop and produce a summary report of everything moved, every shim retained, and every duplicate reconciled.
```

## Prompt 4b — §4 Phase 5: concurrent fault-tolerance & thread isolation

```
Read plan.md §4 PHASE 5 in full (5.1, 5.2, 5.3), plus §3 and §5. Phases 1–4 are merged. Execute PHASE 5 exactly as specified.

5.1 — In backend/claude.py, guard _call_primary_model_sync's asyncio.run() with a loop-state probe (try asyncio.get_running_loop() / except RuntimeError → fast path unchanged). If a running loop IS found, submit asyncio.run(coro) to a dedicated lazily-created module-level ThreadPoolExecutor(max_workers=2, thread_name_prefix="claude-loop-bridge") and block on future.result(timeout=AI_TOTAL_BUDGET_SECONDS); log a warning via logging_setup on that path. Never import app._THREAD_POOL. Tests per plan §5.1 in tests/test_loop_bridge.py (fast path, bridge path, no thread growth, no ResourceWarning, timeout propagation).

5.2 — In backend/sefaria_library.py: migrate the plain TTL dicts (_cache, _search_query_cache, _resolved_title_ref_cache, _resolved_query_ref_cache) to backend.cache.TTLCache, FIRST verifying TTLCache is thread-safe (add an internal threading.Lock to TTLCache if not); preserve the disk write-through. Convert the multi-key caches (_title_catalog_cache, _library_index_view_cache, _library_index_adjustments_cache) to build-new-dict + single atomic reference swap (writer-side lock only; readers lock-free). Lock discipline: never hold a lock across network/disk/JSON work — release, fetch, re-acquire, re-check, write. Treat cached values as immutable after insert; do NOT deepcopy inside critical sections (plan explicitly rejects it) — audit callers and add caller-boundary shallow copies only where a caller mutates. Tests per plan §5.2 in tests/test_sefaria_library_concurrency.py (16-thread hammer, torn-read regression, immutability guard, hit-never-blocks-behind-miss).

5.3 — Exit: full pytest green (including Phase 1–2 golden masters unchanged), no import from app, graphify update . Stop and report.
```

---

## Prompt 5 — §7a: AI source box fix (frontend)

```
Read plan.md §7 bullet "AI source box fix" and .agents/ENGINEERING_RULES.md (mandatory). Fix the AI source-box rendering bugs in the frontend:

- Replace innerHTML += accumulation in the source-card render loop (it destroys earlier nodes' click handlers) with single-insert rendering (build all markup, insert once) and DELEGATED event listeners on the container.
- Make escapeHtml usage consistent across every interpolation point.
- Deduplicate the .ai-source-card CSS (there are duplicate style blocks); one canonical definition.
- Replace the capped nth-child stagger with a per-box CSS custom property (--i) driven stagger so any number of cards animates.
- Add skeleton loading states for the source-box region per ENGINEERING_RULES (zero layout shift, prefers-reduced-motion honored, dark+light themes, WCAG AA contrast).

No backend changes. Verify manually in both themes and confirm existing behavior (links, expansion, citation clicks) is preserved. Run graphify update . after.
```

## Prompt 6 — §7b: observability completion

```
Read plan.md §7 "Observability" and §8.E.1. Complete the observability layer (backend/logging_setup.py, cost_meter.py, SENTRY_DSN already exist — finish, don't rebuild):

- Structured JSON logging with a request ID generated per request and propagated across Flask, thread-pool work (_THREAD_POOL), and asyncio tasks (contextvars).
- Confirm Sentry initializes in prod and _capture_backend_error routes to it with request-ID context.
- Audit that cost_meter.py covers EVERY model call site (Gemini + Claude, sync and async paths) and logs token/USD to Supabase; add any missing call sites; add a daily budget-alert hook.
- No blocking I/O on the FastAPI event loop (asyncio.to_thread or async httpx only, per .agents/ENGINEERING_RULES.md).

Tests: request-ID present in log records across a threaded and an async path; cost meter invoked from each model call site (mocked). Full pytest green; graphify update .
```

## Prompt 7 — §7c: docs pass

```
Read plan.md §7 "Docs pass", §8 "Where this is written down", and §10.1. Write/refresh documentation only (no code changes):

- Update docs/SERVICE_ARCHITECTURE.md to the post-refactor module layout (text_engine, search_provider, ask_pipeline, blueprints).
- Create docs/API.md (every route: method, path, params, auth, response shape — derive from the blueprints and asgi.py), docs/OBSERVABILITY.md (logging/request IDs/Sentry/cost meter/circuit breakers), docs/FRONTEND.md (template/static structure, theming, motion rules).
- A complete environment-variable table (scan the code for every os.environ/os.getenv read; mark required vs optional; never include real values).

Cross-link from README. Keep the style of existing docs/.
```

## Prompt 8 — §7d: motion overhaul

```
Read plan.md §7 "Motion overhaul" and .agents/ENGINEERING_RULES.md (Framer Motion + UI/UX sections are mandatory). Implement the motion system:

- Vanilla `motion` (motion.dev) on the current non-React surfaces; define a motion token layer (durations 150–250ms ease-out, spring configs) in CSS custom properties.
- Zero new @keyframes outside the token layer; migrate existing ad-hoc animations into it.
- Honor prefers-reduced-motion everywhere (guard every animation).
- Micro-interactions per ENGINEERING_RULES: hover/focus-visible/active/disabled feedback on interactive elements; animate only transform/opacity.
- If any React surface exists, use Framer Motion with AnimatePresence + variants + layoutId per the rules; otherwise document the pattern for future React islands.

Verify no CLS regressions and both themes. No backend changes.
```

## Prompt 9 — §7e: dark mode overhaul

```
Read plan.md §7 "Dark mode overhaul" and .agents/ENGINEERING_RULES.md. Replace the ~570 scattered `body.theme-dark` overrides with a semantic token layer:

- Define semantic CSS custom properties (--surface, --surface-raised, --text-primary, --text-muted, --border, --accent, states, etc.) with light values on :root and dark values under the theme class AND @media (prefers-color-scheme: dark) for first paint.
- Add a no-FOUC head script that applies the stored/system theme before first paint.
- Migrate components to tokens incrementally, deleting theme-dark overrides as each component converts; finish with zero scattered overrides (grep-verified).
- AA contrast audit (≥4.5:1 text, ≥3:1 UI) in BOTH themes; never color-only state.
- Use 21st.dev component patterns as the QA baseline for the converted components.

Work in reviewable chunks by template/component group. Verify visually in both themes; no functional regressions.
```

## Prompt 10 — §7f: loading-states overhaul

```
Read plan.md §7 "Loading-states overhaul" and .agents/ENGINEERING_RULES.md. Build the token-driven loading design system (light + dark):

- Skeleton components (shimmer via transform/opacity only) for every region currently showing "Loading..." text; zero layout shift (reserve space).
- Rebuild the AI answer loading animation off hardcoded hex onto the theme tokens (Prompt 9's layer) with dark variants; fix any timer/interval cleanup leaks on unmount/navigation.
- role="status" + aria-live="polite" on loading regions; reduced-motion guards on all shimmer/pulse effects.

Verify in both themes, at 320px and desktop widths, with reduced-motion enabled.
```

## Prompt 11 — §7g: frontend platform fixes (Tailwind build, CSP, SRI, SEO, RTL)

```
Read plan.md §7 "Frontend platform fixes" and §8.C.3–C.4. Implement:

1. Replace the production Tailwind CDN JIT compiler with a proper build step (tailwind.config.js exists): compiled CSS committed or built in CI, CDN script removed from templates, then remove 'unsafe-eval' from CSP.
2. Harden CSP: nonce-based script-src, add Strict-Transport-Security and Permissions-Policy, drop deprecated X-XSS-Protection (headers live in app.py middleware).
3. SRI hashes + version pins on every remaining CDN script/style.
4. Fonts: preconnect + woff2, confirm SILEOT loads efficiently.
5. SEO/OG metadata on public pages; dynamic lang/dir attributes for Hebrew (RTL) pages.

Verify every page still renders (no CSP console violations), scripts load with SRI, and the app works with the compiled Tailwind. This is zero-breakage work: test each template group before moving on.
```

---

## Prompt 12 — §8.A: legal documents & disclosures

```
Read plan.md §8 preamble and §8.A in full. Draft ALL legal documents as attorney-review scaffolding (mark each "DRAFT — pending attorney review", versioned + dated). These are templates rendered via the existing legal-page pattern (components/legal_topbar.html, legal_scripts.html), bilingual en/he like the current pages:

1. Rewrite templates/terms.html complete per §8.A.1 (fill missing §6/§10/§12; all listed clauses: eligibility/age 13+, AI-output disclaimer, not-advice clause, IP/attribution, indemnification, liability cap + AS IS, assumption of risk, dispute resolution placeholder for counsel, DMCA, termination, severability, contact; Last-updated + version).
2. Expand templates/privacy.html per §8.A.2 (data inventory from the actual code: Clerk identity, Supabase bookmarks/preferences/memory summaries, IP for rate limiting, error logs, AI cost logs; purposes/legal bases; processor list; transfers; retention; rights + mechanism; DNS/GPC; children 13+; localStorage disclosure; breach commitment).
3. New templates/ai-disclosure.html per §8.A.3 (LLMs used, RAG at high level, hallucination risk, not-p'sak, consult-your-rabbi, EU AI Act interaction disclosure).
4. New templates/acceptable-use.html, templates/dmca.html, templates/accessibility.html, templates/licenses.html per §8.A.5–A.8.
5. THIRD_PARTY_LICENSES.md at repo root (Sefaria per-text caveat, Hebcal, Wikipedia CC-BY-SA, SILEOT font, MIT JS/CSS libs).
6. Wire versioned click-through consent through the existing /api/accept-legal + accept_legal route: store ToS+Privacy version + timestamp per user; re-prompt on version bump.

Add routes/links for the new pages following the existing legal-page routing. Tests for the consent-version storage. Full pytest green.
```

## Prompt 13 — §8.B: AI liability & religious-guidance safety

```
Read plan.md §8.B in full. Implement the religious-guidance safety layer:

1. Persistent, unavoidable disclaimer in the answer UI: every AI answer renders a visible "Educational information, not a halachic ruling — consult your rabbi" banner (the backend RABBI_FINAL_RULING_FOOTER already exists — surface it prominently; non-dismissible on first use). Bilingual en/he, both themes, token-styled.
2. Extend the scope guardrails in backend/claude.py (_detect_out_of_scope_subject, is_prohibited): medical, legal, mental-health, and dangerous-practice questions dressed as halacha get a referral response (professional + rabbi), never a "ruling".
3. No-impersonation audit: review all system prompts for first-person rabbinic-authority phrasing; add an explicit "never claim to be a rabbi or issue p'sak" rule.
4. Sensitive-topic escalation pattern (minors, abuse, self-harm, medical emergencies): documented referral template, never procedural instruction.
5. Tighten provenance labeling: "internal AI knowledge" answers clearly marked lower-confidence via _build_source_attribution_note.
6. Defensibility logging: request/response/prompt-version metadata retained per the §8.A.2 retention schedule.

Tests: each guarded category returns referral-not-ruling; ordinary halachic Q&A unchanged (no over-refusal regression). Full pytest green.
```

## Prompt 14 — §8.B-AGE: 13+ age gate & age-appropriate output layer

```
Read plan.md §8.B-AGE in full and implement it exactly:

1. AGE_APPROPRIATE_DIRECTIVE constant (single source of truth) appended to both CORE_SYSTEM_PROMPT and SIMPLE_SYSTEM_PROMPT in backend/claude.py, with the content rules in §B-AGE.1 (13+ reader assumption; clinical educational language for sensitive halacha; principles + sources + "learn practical details with a rabbi/teacher/parent"; no explicit detail; scholarly depth preserved).
2. classify_safety(query) step (extending _detect_out_of_scope_subject) run before synthesis, returning ok | sensitive_intimate | medical | mental_health_or_self_harm | abuse_or_minor_safety | dangerous_or_illegal, with the routing behavior in §B-AGE.2.
3. Structured-output fields age_safe + safety_class added backward-compatibly (defaults true/"ok" when absent); route/UI renders referral template when safety_class != ok.
4. Post-generation output check extending validate_model_output: scan for explicit-content markers; on hit, replace body with principles-plus-referral template. Never rely on the prompt alone.
5. Applies across all modes (balanced/practical/sources/strict); bilingual en/he templates matching render_structured_markdown.
6. Age gate at sign-up: 13+ attestation (EU 16+ default) tied to Clerk, stored with the consent record; under-age attempts blocked.
7. docs/AGE_AND_SAFETY_POLICY.md + add the standing rule to .agents/ENGINEERING_RULES.md.

Tests per §B-AGE: tests/test_safety_classifier.py (routing per class; medical/self-harm/abuse never get a "ruling", always a referral; dangerous refuses); intimate-topic fixtures return principles+citations+referral; regression that ordinary Shabbat/kashrut/brachot questions keep full depth; Hebrew parity. Full pytest green.
```

## Prompt 15 — §8.C: security hardening

```
Read plan.md §8.C in full. Execute the security hardening pass:

1. Secrets: scan git history for committed secrets (gitleaks/detect-secrets); confirm .env never tracked; report any key that ever touched a commit (flag for rotation — do not rotate yourself); verify FLASK_SECRET_KEY handling requires a strong value in prod; confirm anon vs service-role Supabase key separation (service-role never reaches the client).
2. Supabase RLS: make STRICT_SUPABASE_RLS the enforced default in prod; add the /api/devtools/rls-audit check to CI; verify every user-data table (bookmarks, preferences, memory summaries) denies cross-user access using scripts/sql/SUPABASE_RLS_POLICIES.sql.
3. CSP/headers: (coordinate with Prompt 11) remove 'unsafe-eval', nonce-based script-src, HSTS, Permissions-Policy, drop X-XSS-Protection.
4. Supply chain: SRI/pin CDN scripts; add pip-audit to CI; extend .pre-commit-config.yaml with secret scanning.
5. Input validation & abuse: audit all route inputs (coordinate bounds, ref decoding, payload caps); confirm both rate limiters active in prod; document the serverless per-instance-limit weakness and the shared-store option.
6. AuthN/Z review: _verify_clerk_token path, CLERK_ENFORCE_AUTH on for protected routes in prod, session cookie flags (Secure/HttpOnly/SameSite) in apply_session_cookie_policy.
7. Finish with a written findings report in docs/SECURITY.md including a vulnerability-report contact.

Zero-breakage: full pytest green after each change; nothing behavior-changing ships without a test.
```

## Prompt 16 — §8.D: privacy operations

```
Read plan.md §8.D in full. Implement privacy operations:

1. DSR flow: self-serve "download my data" (JSON export of the user's Supabase rows) and "delete my account + data" in settings — deletion cascades through all user Supabase tables AND Clerk (use Clerk's deletion API); plus a documented email-intake procedure in docs/PRIVACY_OPERATIONS.md.
2. Consent records: (with Prompt 12) version + timestamp stored at acceptance via /api/accept-legal; re-prompt on material version change.
3. docs/PRIVACY_OPERATIONS.md: DPA checklist (Anthropic, Google, Clerk, Supabase, Vercel, Sentry — link each click-through DPA), Records of Processing (GDPR Art. 30) table, and a basic DPIA in docs/DPIA.md covering automated religious guidance.
4. Retention enforcement: a scheduled job (Vercel cron or Supabase scheduled function) that actually deletes data past the retention windows defined in the Privacy Policy.
5. Breach response plan in docs/PRIVACY_OPERATIONS.md: roles, GDPR 72h timeline, notification templates.

Tests: export returns exactly the user's own data (RLS respected); deletion removes all rows + is idempotent; retention job deletes only past-window rows. Full pytest green.
```

## Prompt 17 — §8.E: reliability, observability & operations

```
Read plan.md §8.E in full (overlaps Prompt 6 — skip anything already done). Implement:

1. Verify Sentry-in-prod, request-ID logging, cost metering on every model call, and circuit breakers on all external calls (Phases 3 / Prompt 6 outputs) — close any gaps.
2. Uptime: document external monitor setup for /api/health and /api/stack/health with alerting (provider-agnostic instructions in docs/RUNBOOKS.md).
3. Graceful degradation: end-to-end verify the fail-open local-corpus path; audit user-facing error states — friendly messages, no stack traces or internals leaked on any 4xx/5xx.
4. Backups: document Supabase backup cadence + restore procedure + customs/config data export in docs/RUNBOOKS.md.
5. Runbooks: incident response, rollback (Vercel preview → promote), deploy checklist — all in docs/RUNBOOKS.md.
6. Cost ceiling: rate limits verified + document hard monthly spend caps on Anthropic/Google dashboards; wire the cost_meter daily budget alert.

Tests for the degradation path (all providers circuit-open still returns a well-formed answer payload). Full pytest green.
```

## Prompt 18 — §8.F: quality, accessibility & content integrity

```
Read plan.md §8.F in full. Implement:

1. CI coverage gate: GitHub Actions workflow running the offline pytest suite with a coverage threshold (start at current coverage, ratchet up); include the RLS audit (Prompt 15) and pip-audit.
2. Accessibility: add axe-core or pa11y automated checks to CI for key pages; fix violations to WCAG 2.1 AA; publish templates/accessibility.html (from Prompt 12) with real audit results.
3. Content integrity: write docs/CONTENT_QA.md stating plainly the app is not rabbinically supervised (or documenting the advisor's scope if one exists) and the review process for corpus/customs data.
4. Localization: verify dynamic lang/dir for Hebrew pages (with Prompt 11), and that machine-translated content is labeled as such, never presented as authoritative.

Full pytest green; CI workflow passes on a test branch.
```

## Prompt 19 — §8.G + §8.H: business scaffolding & launch gate

```
Read plan.md §8.G and §8.H. This is documentation only (the G items are attorney/accountant decisions — document, don't decide):

1. docs/LAUNCH_CHECKLIST.md: reproduce the §8.H checklist verbatim as a living tracked checklist, each line linked to the doc/PR/test that proves it, with current status honestly marked.
2. Add a "Business & compliance — needs human counsel" section listing §8.G items (entity + E&O/cyber insurance, trademark clearance for "Sh'elah", EU AI Act / EAA posture, future-monetization triggers) with plain-language explanations of why each matters.
3. Cross-link from README and docs/SECURITY.md / PRIVACY_OPERATIONS.md.

No code changes.
```

---

## Prompt 20 — §9: agentic AI tool-use layer

```
Read plan.md §9 in full (9.1–9.7) before any code. Implement the agentic tool-use layer behind the AI_AGENTIC_TOOLS flag (env-default OFF; when off, behavior is byte-for-byte today's pre-fetch RAG path).

1. backend/ai_tools.py — pure-function tool registry with JSON schemas (typed params with bounds: lat/lon ranges, date formats, community enum; descriptions encode the hierarchy "texts first, web last"). Wrap ONLY existing backend functions per the §9.2 and §9.2b tables (search_judaic_texts, get_text_by_ref, search_responsa_external, get_zmanim, get_hebrew_date, get_parasha, get_omer, get_holidays, get_daily_study, web_search, lookup_word_meaning, translate_text, get_commentaries, search_community_customs, get_community_profile, browse_library, search_library, get_prayer_text, get_daily_zmanim_summary, convert_measurements [new small deterministic shiurim table — Chazon Ish vs R' Chaim Naeh], calculate_hebrew_date_math, format_source_citation). Imports backend.* only, never app. Do NOT expose export_answer/save_bookmark as model tools (UI actions only — agent loop stays read-only).
2. web_search scope LOCKED: async_search_wikipedia + the existing allowlist in search.py only. No new provider.
3. Orchestrator-level last-resort gate (§9.3.2): web_search is not in the tool list offered to the model until a texts/library search returned insufficient results this turn. Answers that used web_search get include_web_warning=True and tier-4 attribution; never sole basis for a ruling.
4. Agent loop in backend/ask_pipeline.py (§9.4): tool_use → execute (parallel via asyncio.gather + to_thread, reusing _THREAD_POOL) → tool_result → repeat; hard cap 4 rounds; per-tool timeouts; location from stored user preference else "location required" result (never guess).
5. Integration (§9.5): each tool call is timed out, narrowly caught, health.record_success/failure'd, and fail-open; circuit-open providers hidden from the model that turn; cost_meter records every round; the §B-AGE safety classifier runs BEFORE the loop; tool results (esp. web_search) sanitized as untrusted before re-injection.
6. Tests (§9.6): tests/test_ai_tools.py (shape + param validation per tool, offline) and tests/test_agent_loop.py covering scenarios (a)–(g) in the plan, including flag-off reproducing today's behavior exactly.
7. docs/AI_TOOLS.md + the standing rule added to .agents/ENGINEERING_RULES.md (§9.7).

Full pytest green with the flag both on and off. graphify update . Stop and report; do not enable the flag by default.
```

---

## Prompt 21 — §10.1: README full update

```
Read plan.md §10.1 and the current README.md. Update README.md per the 10-point list:

badges row (MIT, Python version, Vercel, tests/CI once present, "not legal/halachic advice"); screenshots/demo-GIF placeholders with TODO markers + live demo link; feature list refresh (community-lens answers, bilingual EN/HE + RTL, PWA/offline, dark mode; agentic tools only if §9 is merged); new "AI & safety" section (RAG overview, source hierarchy texts→poskim→whitelisted→web-last-resort, educational-only disclaimer, 13+ posture, link to AI Disclosure); keep the architecture diagram current + link docs/SERVICE_ARCHITECTURE.md; RECONCILE the env-var table against the actual code (fix wrong names like RATE_LIMIT_PER_MIN/RATELIMIT_STORAGE_URI to the real RATE_LIMIT_ASK/RATE_LIMIT_DEFAULT etc.; required vs optional; no real values); expanded Testing section (offline pytest, coverage gate, golden-master approach); CONTRIBUTING.md + CODE_OF_CONDUCT.md (create both, standard templates) with links; Credits & licenses section pointing to THIRD_PARTY_LICENSES.md; SECURITY.md link with vulnerability reporting.

Docs only, no code changes. Verify every env var name in the table against an actual os.environ/os.getenv read in the code.
```

## Prompt 22 — §10.2: MIT LICENSE + NOTICES

```
Read plan.md §10.2. Add licensing files:

1. LICENSE at repo root: the exact MIT text in plan.md §10.2 (Copyright (c) 2026 Akiva Yevdayev).
2. THIRD_PARTY_LICENSES.md (if not created by the §8.A prompt — otherwise verify/extend): every third-party source with license + attribution — Sefaria (verify per-text licensing caveat noted), Hebcal, Wikipedia (CC-BY-SA share-alike note), SILEOT.woff font license, DaisyUI/Tailwind/marked/DOMPurify (MIT notices). State explicitly that content retrieved from Sefaria/Hebcal/Wikipedia remains under its own license and is NOT relicensed by this project's MIT license.
3. Set the license field in package.json (and any Python packaging metadata) and add the MIT badge to README.

No code changes.
```

## Prompt 23 — §10.3: GitHub profile update (needs prerequisites)

```
PREREQUISITES (confirm with me before starting): my GitHub username, and either the gh CLI authenticated or the Claude-in-Chrome connector. This task involves PUBLIC writes — show me each change and get explicit approval before publishing anything.

Read plan.md §10.3. Then:
1. Create/update the profile README repo (<username>/<username>, README.md) using the draft in plan.md §10.3 with <username> filled in and tone-checked with me first.
2. Pin the Sh'elah repo (+ up to 5 others I choose).
3. Polish the Sh'elah repo: description, topics (judaism, halacha, torah, flask, fastapi, ai, rag, sefaria, vercel), website link; verify README + LICENSE render.
4. Optional bio/link update — propose, don't apply without approval.
```

## Prompt 24 — §11: Vercel deploy fix verification (fix already implemented)

```
Context: plan.md §11 Option A is already implemented — api/index.py exists (re-exporting the ASGI app) and vercel.json uses modern functions + rewrites + headers. Your job is the verification checklist ONLY (plan.md §11 "Verification"):

1. Validate vercel.json parses and matches the Option A spec.
2. Deploy to a PREVIEW URL (never straight to prod): vercel deploy from the repo root.
3. On the preview: confirm / renders; /api/async/health returns ok; /static/css/ai.css serves from CDN (response headers show it was NOT function-served) with the Cache-Control header; /ask works end-to-end; cold start succeeds (the import chain api/index.py → asgi.py → app.py → backend/* resolves — if a root-relative import fails, the sys.path.insert in api/index.py is the fix, keep it).
4. Report results. Only promote to production after I confirm.
```

---

## Prompt 25 — §12 Phase 6: product surface, content & growth layer

```
Read plan.md §12 in full, including the §12.0 claim-triage table (items marked wrong/already-shipped are NOT in scope — do not build user accounts, answer modes, bookmarks, or export; they exist). Prerequisites: §7 frontend overhauls (tokens, skeletons, source-box fix) and §8.A legal pages merged. All new routes go in backend/ blueprints, never app.py; all UI follows .agents/ENGINEERING_RULES.md.

1. §12.1 About page: templates/about.html on the legal-page pattern (legal_topbar, bilingual en/he, both themes, tokens); honest solo-developer content per the plan; links to (never restates) ToS/Privacy/AI-Disclosure; nav/footer link added.
2. §12.2 Help + glossary: /help user guide (asking a good sh'elah, answer modes, reader features incl. existing Shul Mode, community lens, zmanim tour); glossary page statically generated from a build-time JSON seeded via the existing lexicon engine — no per-view API fan-out. Learning paths are deferred — skip.
3. §12.3 Discoverability: reader breadcrumbs from the get_library_index hierarchy (nav[aria-label="breadcrumb"]); expose search_library's existing category/corpus filters in the search UI; citation deep-links from ai_cited_sources into the reader at the ref (build on the §7 delegated-listener source-box container); verify the customs data path has Phase-3 circuit-breaker fail-open end-to-end and skeleton loading. Semantic search is deferred — skip.
4. §12.4 Feedback loop: per-answer helpful/not-helpful + optional ≤500-char comment; new blueprint endpoint; Supabase answer_feedback table (nullable user_id, question hash, mode/lens/language/fallback/safety_class metadata, verdict, comment, ts) with RLS; rate-limited + sanitized; "no personal details" microcopy; devtools feedback-digest view.
5. §12.5 SEO/analytics: robots.txt (disallow /api/, devtools) + generated sitemap.xml of stable public routes, served static — verify they bypass the §11 rewrite (extend the negative-lookahead if needed); JSON-LD WebSite+Organization on home (no schema spam); consent-gated cookieless analytics per §8.A.4 (Vercel Web Analytics or Plausible — NOT Google Analytics by default), documented in the Privacy Policy; light keyword/copy pass.
6. Respect the §12.6 deferred registry — build none of it.

Tests per §12.7: route tests (200s, content-type, bilingual), feedback endpoint (happy/rate-limit/injection/RLS), citation deep-link href, sitemap schema + JSON-LD validity. Full pytest green; graphify update . Stop and report.
```

---

### Suggested execution order

| Order | Prompts | Track |
|---|---|---|
| 1 | 24 | Unblock deploys (verify the fix) |
| 2 | 1 → 2 → 3 → 4 → 4b | Backend refactor (strictly sequential) |
| 3 | 14 → 13 → 12 | Safety + legal (14 first: §9 depends on the classifier) |
| 4 | 20 | Agentic tools (needs Phases 1–2 module paths + Prompt 14) |
| 5 | 11 → 9 → 10 → 8 → 5 | Frontend (11 first: CSP/Tailwind unblocks 9's tokens) |
| 6 | 6 → 17 → 15 → 16 → 18 | Ops/security/privacy |
| 7 | 25 | Phase 6 product surface (after tracks 3 & 5) |
| 8 | 21 → 22 → 19 → 7 | Docs/licensing/launch gate |
| 9 | 23 | GitHub profile (needs your username + approval) |
