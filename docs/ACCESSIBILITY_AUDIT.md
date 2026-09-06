# Accessibility Audit — Color Contrast (2026-08-01, updated 2026-08-20)

WCAG 2.1 AA contrast audit of the core design tokens in `static/css/tokens.css`,
run as part of the Phase E dark-mode token migration (see `plan.md` §18,
`claude_code_prompts.md` Prompt 9). Ratios are computed directly from the
tokens' hex values using the standard WCAG relative-luminance formula — not
asserted. Thresholds: **4.5:1** for normal text, **3:1** for large text
(≥18px, or ≥14px bold) and for UI-component boundaries essential to
identifying the component (WCAG 1.4.11).

## Method

Script: relative luminance `L = 0.2126R + 0.7152G + 0.0722B` (linearized
per-channel), contrast ratio `(L1 + 0.05) / (L2 + 0.05)`. Run against the
`:root` (light) and `[data-theme="dark"]` (dark) blocks in `tokens.css`.
This checks the token layer itself, not every individual rendered element —
a token pairing that passes here is safe wherever it's used; a token pairing
that fails needs the call site checked for font size/weight before treating
it as a real bug.

### Automated coverage (CI)

This hand-computed token audit is backed by an automated per-element scan:
`npm run test:a11y` runs `pa11y-ci` (WCAG2AA, htmlcs runner) over all 8
registered pages **in both themes** — light via `.pa11yci.json`, dark via
`scripts/a11y_dark_scan.js`, which seeds the same stored-preference key the
app itself reads (`Sh'elahPrefs`) before first paint and asserts the page
actually rendered dark before auditing it. Both halves run inside CI's single
"Accessibility scan" step (`.github/workflows/ci.yml`), and both must report
0 errors for the job to pass: 16 checks total. Dark theme was **not** covered
until 2026-08-20 (plan.md §26.2) — headless Chrome resolves
`prefers-color-scheme` to light in CI, and this app normalizes an absent
stored preference to light regardless, so the original gate silently scanned
light theme twice.

The two layers catch different things and neither replaces the other: this
document reasons about token *pairs* in the abstract; the scan measures what
each element actually renders, which is how the `--surface-2` pairing in
finding 4 below was found — a pairing this table had never listed.

## Light theme

| Pair | Ratio | Needs | Result |
|---|---|---|---|
| `--ink-primary` on `--surface-0` (body text) | 9.68:1 | 4.5:1 | ✅ PASS |
| `--ink-secondary` on `--surface-0` (muted text) | 5.06:1 | 4.5:1 | ✅ PASS (fixed 2026-08-15, was 4.45:1) |
| `--ink-heading` on `--surface-0` (headings) | 15.00:1 | 4.5:1 | ✅ PASS |
| `--ink-on-accent` on `--accent-primary` (button text) | 10.77:1 | 4.5:1 | ✅ PASS |
| `--ink-primary` on `--surface-2` (card text) | 9.90:1 | 4.5:1 | ✅ PASS |
| `--ink-secondary` on `--surface-pill` (chip text) | 4.63:1 | 4.5:1 | ✅ PASS (fixed 2026-08-15, was 4.07:1) |
| `--surface-border` vs `--surface-0` (divider) | 1.46:1 | 3.0:1 | ❌ FAIL (by design — see note) |
| `--accent-primary` on `--surface-0` (links) | 10.07:1 | 4.5:1 | ✅ PASS |
| `--accent-gold` on `--surface-0` (gold accent text) | 1.97:1 | 4.5:1 | ❌ FAIL |

## Dark theme

Recomputed 2026-08-20 against the current token values. The `--ink-secondary`
rows below had been stale since 2026-08-15: they still quoted the ratios for
`#807870`, which Prompt 18 had already replaced with `#888077` and §26.2 has
now replaced with `#999188`.

| Pair | Ratio | Needs | Result |
|---|---|---|---|
| `--ink-primary` on `--surface-0` (body text) | 10.15:1 | 4.5:1 | ✅ PASS |
| `--ink-secondary` on `--surface-0` (muted text) | 6.30:1 | 4.5:1 | ✅ PASS (was 4.51:1 at `#807870`) |
| `--ink-secondary` on `--surface-1` (panel text) | 6.02:1 | 4.5:1 | ✅ PASS (fixed 2026-08-15, was 4.31:1) |
| `--ink-secondary` on `--surface-2` (card/footer text) | 5.43:1 | 4.5:1 | ✅ PASS (fixed 2026-08-20, was 4.34:1) |
| `--ink-heading` on `--surface-0` (headings) | 12.51:1 | 4.5:1 | ✅ PASS |
| `--ink-on-accent` on `--accent-primary` (button text) | 3.12:1 | 4.5:1 | ⚠️ FAIL normal-text / ✅ PASS large-text |
| `--ink-primary` on `--surface-2` (card text) | 8.74:1 | 4.5:1 | ✅ PASS |
| `--ink-secondary` on `--surface-pill` (chip text) | 5.17:1 | 4.5:1 | ✅ PASS (was 3.70:1 at `#807870`) |
| `--ctrl-text` on `--ctrl-bg` (control label) | 5.14:1 | 4.5:1 | ✅ PASS (fixed 2026-08-20, was 4.10:1) |
| `--ctrl-text` on `--ctrl-bg-hover` (hovered control label) | 4.52:1 | 4.5:1 | ✅ PASS (fixed 2026-08-20, was 3.61:1) |
| `--surface-border` vs `--surface-0` (divider) | 1.41:1 | 3.0:1 | ❌ FAIL (by design — see note) |
| `--accent-primary` on `--surface-0` (links) | 5.29:1 | 4.5:1 | ✅ PASS |
| `--accent-gold` on `--surface-0` (gold accent text) | 6.58:1 | 4.5:1 | ✅ PASS |
| `--accent-cream` on `--surface-0` (prominent text) | 13.44:1 | 4.5:1 | ✅ PASS |

## Findings requiring follow-up

1. **`--surface-border` vs `--surface-0` fails 3:1 in both themes** (1.46:1 /
   1.41:1). This is a low-contrast *content divider* (`.reader-source-clean`,
   card hairlines), not a UI-component boundary that a user needs to perceive
   to operate the control — WCAG 1.4.11 applies to things like unstyled
   button/input borders, not decorative separators. Not treated as a bug, but
   flagged since anything using `--surface-border` as a *functional* control
   outline (vs. a decorative divider) should use a higher-contrast token
   instead (e.g. `--ctrl-border`/`--control-border`).

2. **`--accent-gold` on `--surface-0` fails badly in light mode** (1.97:1) —
   **resolved 2026-08-15 as documentation, not a code fix.** `--accent-gold`
   is used across the app only for icons, borders, and accents (not
   paragraph text) — grep confirmed no `color: var(--accent-gold)` site
   renders normal body text at this pairing today, so there was no live
   element to change. Added an inline warning comment at the token
   definition (`static/css/tokens.css`) so a future component author doesn't
   introduce the violation.

3. **`--ink-secondary` marginal fails — resolved 2026-08-15.** Light
   `--ink-secondary` on `--surface-0`/`--surface-pill` (was 4.45:1 / 4.07:1)
   is a **live** bug: call-site audit (`static/css/{calendar,reader}.css`)
   found several non-bold, sub-14px consumers (`.cal-hebrew-date` 11.2px,
   `#readerSubtitle` 13.76px, `.segment-badge` 10.88px/600-weight,
   `.reader-source-title-ref` 10.56px/700-weight) that do not qualify for
   the large-text 3:1 exception under this doc's own methodology, so they
   needed the full 4.5:1. Darkened `--ink-secondary` from `#64748b` to
   `#5a6b85` (light theme only — dark theme's `#807870` passed at 4.51:1 on
   `--surface-0`, the only dark surface this pass checked it against, and was
   left untouched; that "already passes" conclusion turned out to be scoped
   to one surface and was overturned twice, see finding 4). New light-theme
   ratios: 5.06:1 on `--surface-0`, 4.63:1 on `--surface-pill`, both with
   margin. Visually still clearly lighter than
   `--ink-primary` (9.68:1→ still ~2x the contrast of secondary text), so
   the muted/secondary hierarchy is preserved — verified in the browser
   preview, both themes, see below.

   **`--ink-on-accent` on `--accent-primary` (dark mode, 3.12:1) — no fix
   needed.** Repo-wide grep (CSS, templates, JS) found **zero consumers** of
   `var(--ink-on-accent)` anywhere — the token is defined but unused. Left
   as-is; flagging here so a future consumer knows to check contrast before
   using it for normal-weight text on the accent surface.

4. **Dark-theme muted text failed on the surfaces nobody had measured —
   resolved 2026-08-20** (plan.md §26.2). The first-ever dark-theme pa11y run
   flagged 16 elements on `/`: 15 at 4.34:1 (`--ink-secondary` `#888077` on
   `--surface-2` `#1e1d1a` — the Popular Texts card subtitles and the site
   footer's disclaimer text) and 1 at 4.10:1 (`--ctrl-text` `#888070` on
   `--surface-pill` `#23211f` — the topbar language button). Both are
   normal-weight, sub-14px text, so neither qualifies for the large-text 3:1
   exception.

   This is the **third** time `--ink-secondary` has been lightened, and the
   pattern is the finding: each previous pass fixed only the one surface it
   had measured (`#807870`→`#888077` for `--surface-1` in Prompt 18, and
   before that a light-theme-only fix that explicitly declared dark "already
   passes" based on `--surface-0` alone). So this pass checked the new values
   against **every** dark surface in the palette, hover states included, and
   the token comments in `static/css/tokens.css` now carry that full table.
   `--ink-secondary` → `#999188`, `--ctrl-text` → `#999181`; both cleared
   4.5:1 everywhere with margin, and the six duplicated literals across the
   `[data-theme="dark"]`, `prefers-color-scheme` and legacy-alias blocks were
   all updated together (a `grep` for the old hexes returns only comments).
   Cost: `--ink-secondary`'s separation from `--ink-primary` narrows from
   2.02:1 to 1.61:1 — still plainly a muted tier, and preferable to a fourth
   round of clearing the threshold by hundredths.

## Not covered here

- Individual component-level contrast (e.g. badge text-on-background pairs
  added during the ai.css token migration) — spot-checked during that work
  but not exhaustively re-verified against WCAG ratios in this pass.
- Focus-ring visibility, non-color state indicators, and motion/reduced-motion
  behavior — covered separately by the existing `:focus-visible` rules and
  `prefers-reduced-motion` guards audited in Phase D.
