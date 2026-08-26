# Sh'elah — Mandatory Styling & Engineering Directives

These rules apply to ALL future agent tasks in this repository. They are non-negotiable defaults unless the user explicitly overrides them.

## UI/UX Pro Max Principles

1. **Responsive layouts** — every view must work from 320px to 4K. Use fluid grids (CSS Grid / flex), `clamp()` for typography, and container queries where supported. No fixed pixel widths on layout containers.
2. **Skeleton loading states** — any async data region (AI answers, library texts, zmanim, calendar) must render a layout-stable skeleton, never a blank area or layout shift. Reserve space matching the final content's dimensions (CLS ≈ 0).
3. **Accessible color contrast** — all text must meet WCAG 2.1 AA: ≥ 4.5:1 normal text, ≥ 3:1 large text and UI components. Verify both light and dark themes. Never convey state by color alone.
4. **Pixel-perfect alignment** — all spacing on a 4px/8px scale via CSS custom properties. Optical alignment of icons with text baselines. No ad-hoc magic-number margins.
5. **Reactive micro-interactions** — interactive elements must give immediate feedback (hover, focus-visible, active, disabled states). Transitions 150–250ms, ease-out. Always honor `prefers-reduced-motion`.
6. **Touch targets** — minimum 44×44px interactive area on touch devices.
7. **Keyboard & screen reader** — full keyboard operability, visible focus rings, correct ARIA roles/labels, semantic HTML first.

## Framer Motion Skills (for any future React surfaces)

1. **Fluid spring physics** — prefer `type: "spring"` with tuned `stiffness`/`damping` over duration-based tweens for movement; use tweens only for opacity/color.
2. **Exit/entry animations** — wrap conditionally rendered elements in `<AnimatePresence>` with explicit `initial` / `animate` / `exit` variants. Never unmount animated content abruptly.
3. **Shared layout transitions** — use `layoutId` for elements that morph across views (tabs, cards, modals) and `layout` props for reflow animations.
4. **Performance guards** —
   - Animate only `transform` and `opacity`; never animate layout properties (width/height/top/left) per-frame.
   - Use `useMotionValue` / `useTransform` instead of state-driven animation to avoid React re-renders on every frame.
   - Memoize variant objects outside components; never define them inline in render.
   - Wrap animation-heavy children in `React.memo`; isolate `motion` components so parent re-renders don't restart animations.
   - Use `will-change` sparingly and remove it after animation completes.
5. **Reduced motion** — respect `useReducedMotion()`; provide non-animated equivalents.

## Component & Motion Tooling (mandatory from roadmap Phase 3 onward)

### 21st.dev rule set (UI changes, including dark mode)
1. Before hand-building any non-trivial UI element (card, modal, command palette, settings panel, toast, skeleton, data table), check 21st.dev's component registry for an established pattern and adapt its markup/Tailwind classes — including its `dark:` variants, mapped onto our `[data-theme="dark"]` token layer.
2. Dark-mode component work uses 21st.dev dark-theme components as the visual QA baseline.
3. When React surfaces exist, install 21st.dev components directly (shadcn-compatible registry) instead of re-implementing them; use the 21st.dev Magic MCP for component generation via tool calls rather than freehand markup.
4. Caveat to respect: 21st.dev components are React-first — on vanilla surfaces, adapt patterns/markup only; do not introduce React solely to consume a component.

### Loading states (all loading animations, screens, and AI loading — light AND dark)
1. Every loading visual (skeletons, spinners, overlays, AI staged-progress, shimmer) draws colors exclusively from semantic loading tokens defined for both `:root` and `[data-theme="dark"]`. Hardcoded hex values or Tailwind arbitrary-color classes (`bg-[#…]`) in loading UI are forbidden.
2. Async regions use content-shaped skeletons matching final dimensions (CLS ≈ 0). Bare "Loading..." text placeholders are forbidden.
3. Pick the right primitive: skeleton for content regions, spinner for short indeterminate waits, staged progress (phase text + indicators) for long AI operations.
4. Full-screen overlays appear only after a ~150ms delay (no flash on fast loads) and animate out before content is revealed.
5. Any loading sequence that schedules timers/intervals (cycling phase text, staggers) must cancel them on resolve, reject, and abort — zero orphaned timers.
6. All loading animations honor `prefers-reduced-motion` (static skeleton, no shimmer/bounce/spin), animate `transform`/`opacity` only, and share one shimmer/spin keyframe set from the loading layer — no per-feature duplicates.
7. Accessibility: pending regions set `role="status"` + `aria-busy="true"`; cycling status text uses `aria-live="polite"`; decorative skeletons/spinners are `aria-hidden="true"`; overlays never steal focus.
8. Every loading state is visually verified in BOTH light and dark themes (screenshot pair) before sign-off, QA'd against 21st.dev skeleton/loader patterns.

### Framer Motion tool-call workflow (all motion changes)
1. Every motion task goes through the UI/UX skill toolchain (`ui-ux-pro-max` carries the Framer Motion stack guidance); no freehand animation code.
2. React surfaces: Framer Motion APIs exclusively — `<motion.*>`, `AnimatePresence`, `layoutId`, `useMotionValue`/`useTransform`, `useReducedMotion`. No raw CSS `@keyframes`, no rAF loops.
3. Vanilla surfaces (current codebase): the `motion` (motion.dev) API is the mandated call surface — `animate()`, `spring()`, `stagger()`, `inView()` — chosen because each call maps 1:1 to a Framer Motion equivalent for future React migration.
4. PR gate: zero new `@keyframes` outside `tokens.css`; zero new `transition` rules on transform properties; all entrance/exit animation handles element removal gracefully (vanilla equivalent of `AnimatePresence`).

## AI request resilience & source integrity (mandatory for any `/ask`, model-call, or source-box change)

These rules exist because of two confirmed production bugs (see `plan.md` §23.4): the ASGI `/ask` handler silently dropped `ai_cited_sources`, and the browser aborted `/ask` at 10 s with no retry while the server pipeline + model retries needed longer. Do not reintroduce either class of defect.

### Timeout & retry (no premature abort)
1. **Three coordinated budgets, all env-configurable.** Per-model-call timeout (`AI_MODEL_TIMEOUT_SECONDS`) < total server request budget (`AI_TOTAL_BUDGET_SECONDS`) < client abort ceiling < the platform (Vercel `functions.maxDuration`) ceiling. Never hardcode a timeout that violates this ordering.
2. **The client must not give up before the server has had a bounded, fair chance.** The browser `/ask` request retries automatically (bounded, with backoff) on abort/network/`5xx`; it never aborts on the first try. A single fixed `setTimeout(abort, 10000)` with no retry is forbidden.
3. **The server fails *gracefully*, never hangs and never 500s on timeout.** Wrap model synthesis in `asyncio.wait_for(…, AI_TOTAL_BUDGET_SECONDS)`; on timeout fall through to the existing source-discovery fallback ladder and return a `200` with `meta.fallback=true`.
4. **Provider retry ladders must fit inside the server budget.** Any `tenacity`/SDK retry (`stop_after_attempt`, exponential backoff) must have a worst-case total duration provably below `AI_TOTAL_BUDGET_SECONDS`.
5. **No dead timeout config.** Every timeout/retry constant must actually be passed to the client/SDK it names (e.g. `AsyncAnthropic(timeout=…, max_retries=…)`). A defined-but-unused knob is a bug.
6. **No orphaned timers** on the client: every abort/phase/stagger timer is cleared on resolve, reject, and abort (see Loading-state rule 5).

### Source-display integrity
7. **`/ask` response-schema parity across transports.** The Flask (`app.py`) and ASGI (`asgi.py`) `/ask` handlers must return the **same JSON key set on every path** — success, strict-mode block, and fallback. Build the payload through one shared builder so the two transports cannot drift. Adding a key to one handler without the other is forbidden. `/ask` is implemented twice (Flask sync, FastAPI async) by deliberate decision (plan.md §22). Any change to one MUST be mirrored in the other in the same commit, and the parity suite must be extended if the change adds a new observable behavior.
8. **AI-cited sources must always reach the client.** `ai_cited_sources` (derived from `structured.sources`) is always present in the response (`[]` when none). The source box renders the AI's *actually-cited* references as the authoritative set; retrieved/keyword-ranked sources only enrich or supplement them, never replace them. "Show the right sources" means the answer's own citations, not a re-ranking of whatever was retrieved.
9. **Single render write, single handler wire.** Source boxes are built as one accumulated HTML string and written once; click handlers are wired once after the final write (or via one delegated listener). `innerHTML +=` inside a render loop is forbidden — it orphans listeners on already-rendered cards.
10. **Verify before done:** golden-master `/ask` fixtures assert the response key set and `ai_cited_sources` contents on every path; manual check confirms cited sources render with text and working "Open in Reader" links in both light and dark themes.

## Age-appropriate output & safety routing (mandatory for any system-prompt, `claude.py`, or safety-classification change)

All AI output must be suitable for a 13+ reader (`plan.md` §8.B-AGE; see `docs/AGE_AND_SAFETY_POLICY.md`). Sensitive halacha (niddah, mikveh, intimacy) gets principles + sources + "learn the practical details with a rabbi/teacher/parent," never explicit detail — this constrains tone only, never scholarly depth. `classify_safety()` in `backend/claude.py` runs before every synthesis call; `medical`, `mental_health_or_self_harm`, and `abuse_or_minor_safety` route to a professional/rabbi referral instead of a ruling and never reach the model. Defense in depth is mandatory: the `AGE_APPROPRIATE_DIRECTIVE` system-prompt layer is not sufficient alone — `validate_model_output()`'s post-generation explicit-content check is the required second layer. Any change to the safety-routing patterns must keep the "no over-refusal" regression (ordinary Shabbat/kashrut/brachot questions stay `ok`-classified) green in `tests/test_safety_classifier.py`.

## AI tool-use & agentic layer (mandatory for any `backend/ai_tools.py` or `backend/ask_pipeline.py` change)

AI tool-use honors the source hierarchy — Judaic texts & computed calendar/zmanim first; `web_search` is last-resort, orchestrator-gated, never the sole basis for a halachic ruling; deterministic data (zmanim, dates, parasha, omer) always comes from the computation engines, never the model. See `docs/AI_TOOLS.md` for the full catalog, the gate mechanics, and the `AI_AGENTIC_TOOLS` flag.

## Cost & caching (mandatory for any new/changed `/api/*` route, or anything that fetches on an interval)

Vercel bills Provisioned Memory for the full wall-clock lifetime of every invocation and Fast Origin Transfer per byte served from the function — see `docs/VERCEL_COST_OPTIMIZATION.md` for the full model. Two rules keep both meters bounded:

- **Cache-Control is a classification, not a per-route decision.** Every `/api/*` GET route's `Cache-Control` header comes from `backend/cache_policy.py::classify_cache_tier()` — add new routes to that module's tier tables (immutable/dated/corpus/private), never set the header ad hoc in a route handler. A response is only eligible for a public tier if it is a **pure function of the URL** (method + path + query params) — if a handler falls back to `session`/cookie/IP-derived state when a param is absent, it must set `flask.g.cache_tier_force_private = True` before returning (see `backend/routes_calendar.py`'s zmanim/holidays session-fallback branches), or that response can leak one user's session-derived data to another user via the shared CDN cache.
- **No warmers, no unconditional polling.** Do not add a cron job, `setInterval`, or client-side keep-alive that pings any `/api/*` or `/ask` route on a fixed interval "to keep it warm" — Vercel Fluid compute has no cold-start benefit from this and it only burns invocations/Active CPU for nothing. A polling interval is only acceptable when gated behind an explicit user action (e.g. `templates/index.html`'s devtools-inspector heartbeat, which starts on `openDevtoolsInspector()` and is cleared on close) — never running unconditionally for every visitor.

## Rate limiting & abuse mitigation (mandatory for any new route, or any change to `backend/rate_limit.py`/`backend/turnstile.py`)

- **Every new route must be classified in the policy table.** `backend/rate_limit.py::_POLICIES` is the single source of truth for which class (`llm`, `cheap`, or a new one) governs a route's request budget. A route reachable from either transport (`app.py`'s Flask mount or `asgi.py`'s native FastAPI routes) that is not classified gets whatever the default policy allows — silently, with no test forcing a decision. Classify it explicitly before merging, and add a test proving the classification is enforced (matching `tests/test_ask.py::TestAskRateLimit`'s pattern) rather than trusting the default.
- **Identity-aware keys, not bare IP.** Key on the Clerk `sub` when authenticated, the trusted-IP key otherwise (`backend/rate_limit.py`'s reuse of `backend/auth.py`'s existing token verification — never re-implement JWT parsing at a new call site). IP-only buckets punish shared-egress traffic (a yeshiva, day school, or shul behind one CGNAT IP) as if it were a single abusive caller.
- **Fail-open/fail-closed posture is a deliberate per-class decision, not a default.** The `llm` class fails **closed** on a store outage — an unmetered `/ask` during an outage is a budget hole. Every other class fails **open** — a reader should not be blocked by a Redis blip. State which posture a new class uses and why in a code comment; do not assume one without deciding.
- **Reuse the shared store.** Any new cross-instance-safe counter (a new mitigation, a new threshold) goes through `backend.rate_limit.get_shared_store()` under its own key namespace — never open a second Redis connection or a second in-process store for the same cross-instance-safety need.
- **Log every mitigation action, hash the key.** Any code path that rejects a request for rate-limit/bot-mitigation reasons calls `backend.logging_setup.log_mitigation(tier, route_class, key_hash, route)` — one structured log line plus a Sentry **breadcrumb** (never a Sentry event; routine 429s would drown the error signal and burn the free-tier event quota). Never log a raw IP or Clerk `sub` — hash it first (§8.D privacy).

## General engineering

- Zero-breakage rule: no change ships without a verification step (manual route check or test).
- Python: async safety first — never call blocking I/O inside the FastAPI event loop; use `asyncio.to_thread` or httpx async clients.
- Keep `app.py` from growing: new routes belong in blueprints/routers under `backend/`.
