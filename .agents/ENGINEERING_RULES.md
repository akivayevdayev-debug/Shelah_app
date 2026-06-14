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

## General engineering

- Zero-breakage rule: no change ships without a verification step (manual route check or test).
- Python: async safety first — never call blocking I/O inside the FastAPI event loop; use `asyncio.to_thread` or httpx async clients.
- Keep `app.py` from growing: new routes belong in blueprints/routers under `backend/`.
