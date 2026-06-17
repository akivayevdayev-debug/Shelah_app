# graphify
- **graphify** (`.claude/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.

# Mandatory styling & engineering directives

All tasks MUST follow `.agents/ENGINEERING_RULES.md`. Summary of the non-negotiables:

## UI/UX Pro Max
- Responsive layouts (320px → 4K), fluid grids, `clamp()` typography; no fixed-width layout containers.
- Skeleton loading states for every async region; zero layout shift (CLS ≈ 0).
- WCAG 2.1 AA contrast (≥4.5:1 text, ≥3:1 UI), verified in light and dark themes; never color-only state.
- Pixel-perfect alignment on a 4px/8px spacing scale via CSS custom properties; no magic-number margins.
- Reactive micro-interactions: hover/focus-visible/active/disabled feedback, 150–250ms ease-out, honor `prefers-reduced-motion`.
- 44×44px minimum touch targets; full keyboard operability and correct ARIA semantics.

## Framer Motion (any future React surfaces)
- Spring physics (`type: "spring"`) for movement; tweens only for opacity/color.
- All conditional mount/unmount via `<AnimatePresence>` with explicit initial/animate/exit variants.
- Shared layout transitions via `layoutId` / `layout` props for morphing elements.
- Performance guards: animate only `transform`/`opacity`; drive frames with `useMotionValue`/`useTransform` (not React state); hoist variant objects out of render; `React.memo` animation-heavy children; honor `useReducedMotion()`.

## Engineering
- Zero-breakage: every change verified before completion.
- Async safety: no blocking I/O on the FastAPI event loop — `asyncio.to_thread` or async httpx only.
- New routes go in `backend/` routers/blueprints, not `app.py`.
