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

These rules exist because of two confirmed production bugs (see `plan.md` §7.1.A and §7.13): the ASGI `/ask` handler silently dropped `ai_cited_sources`, and the browser aborted `/ask` at 10 s with no retry while the server pipeline + model retries needed longer. Do not reintroduce either class of defect.

### Timeout & retry (no premature abort)
1. **Three coordinated budgets, all env-configurable.** Per-model-call timeout (`AI_MODEL_TIMEOUT_SECONDS`) < total server request budget (`AI_TOTAL_BUDGET_SECONDS`) < client abort ceiling < the platform (Vercel `functions.maxDuration`) ceiling. Never hardcode a timeout that violates this ordering.
2. **The client must not give up before the server has had a bounded, fair chance.** The browser `/ask` request retries automatically (bounded, with backoff) on abort/network/`5xx`; it never aborts on the first try. A single fixed `setTimeout(abort, 10000)` with no retry is forbidden.
3. **The server fails *gracefully*, never hangs and never 500s on timeout.** Wrap model synthesis in `asyncio.wait_for(…, AI_TOTAL_BUDGET_SECONDS)`; on timeout fall through to the existing source-discovery fallback ladder and return a `200` with `meta.fallback=true`.
4. **Provider retry ladders must fit inside the server budget.** Any `tenacity`/SDK retry (`stop_after_attempt`, exponential backoff) must have a worst-case total duration provably below `AI_TOTAL_BUDGET_SECONDS`.
5. **No dead timeout config.** Every timeout/retry constant must actually be passed to the client/SDK it names (e.g. `AsyncAnthropic(timeout=…, max_retries=…)`). A defined-but-unused knob is a bug.
6. **No orphaned timers** on the client: every abort/phase/stagger timer is cleared on resolve, reject, and abort (see Loading-state rule 5).

### Source-display integrity
7. **`/ask` response-schema parity across transports.** The Flask (`app.py`) and ASGI (`asgi.py`) `/ask` handlers must return the **same JSON key set on every path** — success, strict-mode block, and fallback. Build the payload through one shared builder so the two transports cannot drift. Adding a key to one handler without the other is forbidden.
8. **AI-cited sources must always reach the client.** `ai_cited_sources` (derived from `structured.sources`) is always present in the response (`[]` when none). The source box renders the AI's *actually-cited* references as the authoritative set; retrieved/keyword-ranked sources only enrich or supplement them, never replace them. "Show the right sources" means the answer's own citations, not a re-ranking of whatever was retrieved.
9. **Single render write, single handler wire.** Source boxes are built as one accumulated HTML string and written once; click handlers are wired once after the final write (or via one delegated listener). `innerHTML +=` inside a render loop is forbidden — it orphans listeners on already-rendered cards.
10. **Verify before done:** golden-master `/ask` fixtures assert the response key set and `ai_cited_sources` contents on every path; manual check confirms cited sources render with text and working "Open in Reader" links in both light and dark themes.

## General engineering

- Zero-breakage rule: no change ships without a verification step (manual route check or test).
- Python: async safety first — never call blocking I/O inside the FastAPI event loop; use `asyncio.to_thread` or httpx async clients.
- Keep `app.py` from growing: new routes belong in blueprints/routers under `backend/`.
