# Frontend architecture

**Status (2026-10-02):** a single-page shell, `templates/index.html` (about 14,500 lines), plus 31 ES and classic modules under `static/js/`, 16 stylesheets under `static/css/` (plus the legacy `static/style.css`), and a service worker. There is no bundler: modules are served as written, with content-hashed URLs from an import map. Tests: `npm test` runs `node --test tests_js/*.test.js` (31 test files, one per module family).

The shell is migrating, one feature at a time, from its inline classic `<script>` to modules. The inline script still owns the reader, the library tree, view switching and the settings menus; everything newer (conversations, routing, stored answers, the siddur, the calendar detail card, citations) lives in modules.

---

## 1. How the page loads

`templates/index.html` is a Jinja template rendered by `backend/routes_pages.py`. In `<head>`, in order:

1. **No-flash bootstraps** (tiny inline scripts, run before first paint): theme (`data-theme` and `theme-dark` on `<html>`, from `localStorage["Sh'elahPrefs"].theme`, falling back to `prefers-color-scheme`), view transitions (`shelah.viewTransitions`), language and direction (`?lang=`, then the saved preference, then Hebrew by default for an Asia/Jerusalem time zone), a `auth-likely-in` class from the Clerk `__client_uat` cookie, and a `data-cold-view` flag for deep links to a text, prayer, community or history page so the shell shows a skeleton instead of the home grid.
2. **Third-party scripts, each pinned** (SRI hash where the host allows it): Sentry browser bundle, `marked`, `DOMPurify` and FullCalendar from jsDelivr, DaisyUI's stylesheet, Clerk (publishable key from the server), Cloudflare Turnstile, `motion` (vanilla `animate()`), Vercel Speed Insights. The CSP that allows exactly these hosts is in `docs/SECURITY.md` §3.
3. **Classic helper scripts** that expose window globals the inline script reads: `sentry-init.js`, `topbar_shared.js`, `hebrew-ref.js`, `community-labels.js`, `load-region.js`, `clerk-appearance.js`.
4. **The import map** (`module_import_map`, built by `backend/module_versions.py`): each module's plain URL (`/static/js/router.js`) maps to `/static/js/router.js?v=<12-char sha256 of its bytes>`. A changed module is a new URL and an unchanged one keeps its cached copy, so a CDN or service-worker cache can never hand a new `main.js` an old `router.js`. The entry `<script type="module">` tags (`motion.js`, `calendar-detail.js`, `main.js`) use `module_url(name)` for the same URL, so a module that is both an entry and an import runs once. Classic scripts (no top-level `import`/`export`) are not in the map; they carry a hand-bumped `?v=` query instead.

Stylesheets load in this order (tokens first, always): `tokens.css`, `style.css`, `typography.css`, `reader.css`, `sidebar.css`, `halacha.css`, `prayer.css`, `siddur.css`, `ai.css`, `conversation.css`, `history.css`, `calendar.css`, `calendar-detail.css`, `loading.css`, `tailwind.css`. The legal pages use `legal.css` through `templates/components/legal_*.html`.

### Page zones in `index.html`

| Zone | Element | Notes |
|---|---|---|
| Library navigation | `#leftSidebar` | Text tree, siddur contents, community list |
| Main content | `#mainContainer` | `#homeGrid` (home cards), the reader, prayer/community/history pages |
| Commentary panel | `#rightSidebar` | Commentary on the selected passage or, in "reading" mode, the verse in view |
| Ask Sh'elah | `#convPanel` (`data-size` overlay / full / mini, `data-layout` desktop / mobile), `#convPip`, `#convHistoryPop`, `#mobileSearchTray` | Owned by `conversation-ui.js` |
| Calendar | FullCalendar overlay, plus one `<dialog id="calDetail">` | Detail card owned by `calendar-detail.js` |

Other templates: `404`, `about`, `acceptable-use`, `accessibility`, `ai-disclosure`, `dmca`, `glossary`, `help`, `licenses`, `privacy`, `terms` (static, server-rendered), and `templates/components/` (`icons.html` Jinja macro, `legal_topbar/footer/scripts`, `site_footer`, `theme_bootstrap`).

---

## 2. Module map: `static/js/`

`main.js` is the one entry point that wires the modules to the inline script. It builds explicit `deps` objects from `window.*` in one place (`buildZmanimDeps`, `buildSiddurDeps`) so no module reaches for an undeclared inline global, then assigns the bridge objects in section 3.

### State, routing and bootstrap

| Module | Role |
|---|---|
| `state.js` | Pub/sub store (section 4) |
| `router.js` | Deep-link router: path and query grammar, `readRoute`, `pushRoute`, `pushPath`, `clearRoute`, `closeOverlay`, `installRouter` (popstate). Exposed as `window.ShelahRouter` |
| `main.js` | Imports the modules above, installs the router and conversation UI, exposes the bridges, calls `hydrateRoute(readRoute())` once |
| `load-region.js` (classic) | One loading/failure/retry contract for every async region (section 7). `window.ShelahLoadRegion` |
| `topbar_shared.js`, `hebrew-ref.js`, `community-labels.js` (classic) | Shared topbar helpers (also used by the legal pages), Hebrew numerals and verse-accurate parashah lookup, Hebrew names for the community-customs data |
| `sentry-init.js` (classic) | Sentry browser init with the privacy and quota rules (drops ResizeObserver noise, scrubs PII); its `/api/client-errors` companion is in `reader-ui.js` |
| `clerk-appearance.js` (classic) | Builds Clerk's `appearance` from the live tokens for the active theme (Clerk wants concrete colours, not `var(--token)`) |

### Asking questions

| Module | Role |
|---|---|
| `ai-service.js` | `askAi(question, options)`: the canonical `POST /ask` client. 3 attempts, 60 s per attempt, backoff 1200 ms times the attempt number, retrying only 502/503/504 and network `AbortError`/`TypeError`; one auth-header refresh and retry on 401; never retries a 4xx. Asks for `application/x-ndjson` and reports each pipeline step to `options.onProgress`; a server that answers plain JSON is handled the same. Throws an `Error` with `.status`, `.code` (for example `turnstile_required`), `.attempts` |
| `ask-progress.js` | Turns the NDJSON progress lines into a small state object and view; owns no DOM |
| `conversation-entry.js` | Which Ask surface a question opens: signed in uses the multi-turn conversation UI, signed out uses a one-shot `/ask` answer in the same panel with a sign-in prompt in place of the composer |
| `conversation-api.js` | Network client for `/api/conversations/*`; throws `ConversationApiError` with a `.code` the UI branches on |
| `conversation-store.js` | View-model for one thread: header, transcript, the "sources consulted" disclosure, the sidebar list. Owns no DOM; every surface renders from `getState()` |
| `conversation-ui.js` | Renders the store into `#convPanel` and wires every entry point (topbar Ask button, phone tray, history popover, one-time discovery tip). Exposed as `window.ShelahConversationUI` |
| `conversation-ui-helpers.js` | Pure helpers for the above (no DOM) |
| `source-cards.js` | Pure string builders for source cards, shared by every turn |
| `citation-markers.js`, `citation-popover.js` | The `[n]` chips after a claim, and the card a chip opens (source, one-line note, first lines, open-in-reader or jump to the list row) |
| `answer-link.js`, `answer-share.js` | "Copy link" for a stored answer (owner link `/answer/<id>`) and public share links (`/a/<token>`, `POST …/share`, revoke) |
| `ask-history.js` | The `/history` page (keyset-paged `GET /api/user/history`, searchable) and the shelf's stored-answer entries. `window.ShelahAskHistory` |

### Reading

| Module | Role |
|---|---|
| `reader-ui.js` | Global error boundary (posts to `/api/client-errors`), semantic bookmarking (`/api/bookmarks/semantic`), and the reader's small helpers |
| `commentary-preload.js` | Finds the verse at the reading line and preloads its commentary before the reader selects anything. `window.ShelahCommentaryPreload` |
| `siddur.js`, `siddur-reader.js`, `siddur-day.js` | The siddur at `/siddur/<rite>[/<service>[/<section>]]` (section 8) |
| `zmanim.js` | The zman clock and the daily-study prefetch (section 9) |
| `calendar-detail.js` | Apple-style detail card for a calendar day or event: an anchored popover on desktop, a sheet on phones. `window.ShelahCalendarDetail = { open, close, isOpen }` |

### Presentation

| Module | Role |
|---|---|
| `motion.js` | `window.ShelahMotion` (section 6) |
| `icons.js` | Phosphor glyphs for markup built in JS; same set and viewBox as `templates/components/icons.html`. Add a glyph only by copying it from Phosphor, never draw one |

The AI mark (the star, the Ask button, the glow) is custom and is **not** a Phosphor glyph; leave it out of any icon-consistency pass.

---

## 3. Bridges between the inline script and the modules

Window globals are the documented seam. Anything the inline script needs from a module is exposed in one place:

| Global | Set by | Purpose |
|---|---|---|
| `ShelahModules` | `main.js` | `askAi`, `loadSemanticBookmarks`, `saveSemanticBookmark`, `prewarmDailyStudy`, the zmanim functions, and `getState`/`setState`/`subscribe` |
| `ShelahState` | `state.js` | `getState`, `setState`, `subscribe` (never the raw object) |
| `ShelahRouter` | `router.js` | the router API; the inline script calls `pushRoute`/`clearRoute` from its view switches and `main.js` installs `window.hydrateRoute` |
| `ShelahConversationUI` / `ShelahConversationEntry` | `conversation-ui.js` / `conversation-entry.js` | open the panel, choose the Ask surface |
| `ShelahSiddur` | `main.js` (`createSiddur`) | `prepare` / `mount` for the inline `displaySiddur()` |
| `ShelahSourceCards`, `ShelahCommentaryPreload`, `ShelahAskHistory`, `ShelahAnswerLink` | `main.js` | the module APIs the inline script calls |
| `ShelahPrefs` | `index.html` | `set('mode' \| 'community', value)`, called by the chat settings menu to save the defaults |
| `ShelahMotion`, `ShelahCalendarDetail`, `ShelahLoadRegion`, `ShelahHebrewRef`, `ShelahClerkAppearance` | their own files | as named |

When the last inline caller of a bridged function is gone, delete its entry rather than leaving it.

---

## 4. State

`state.js` exports `getState()`, `setState(patch)` (deep-merges plain objects, then notifies) and `subscribe(listener)` (returns an unsubscribe function; a throwing listener never breaks the others). The store **is** `window.appState`: the inline script builds `appState` first, and `state.js` adopts it when present, otherwise it starts with the small default shape below.

What the modules read and write:

| Key | Description |
|---|---|
| `prefs` | `mode` (`balanced` / `practical` / `sources` / `strict`), `community` (`All` or a community), `readerLayout` (`bilingual` / `bilingual-reverse` / `interleaved` / `english` / `hebrew`), `fontSize`, `lineHeight`, `showLeftSidebar`, `showRightSidebar`, `metadataFilters`, `transliteration`, `showVowels`, `showCantillation`, `compareMode`/`compareRef`, `shulMode` (+ auto-scroll and speed), `theme` (`light` / `dark` / `system`) |
| `ai` | `pending`, `lastResponse`, `lastError` (written by `askAi`) |
| `dailyStudy` | `refs`, `prewarmedAt` |
| `semanticBookmarks` | `items`, `lastUpdatedAt` |
| `currentView`, `shownView` | The page on screen (inline script) |
| `lastAiQuestion`, `lastAiResponse`, `readingState`, `reliability` | Inline-script state |

Preferences persist in `localStorage["Sh'elahPrefs"]` and, when signed in, sync with Supabase through `/api/user/preferences`. Nothing else holds a copy of shared data.

---

## 5. Routing

`router.js` owns the URL. Everything the app writes lives in the **path**; only a shared link's display keys and the history search stay in the query. A path is an optional view followed by tail segments:

```text
view:  /text/<ref>   /prayer/<name>   /community/<name>
       /answer/<uuid>   /a/<share token>   /history
       /siddur/<rite>[/<service>[/<section>]]
tail:  chat/<id|new>          the conversation
       <community>/<mode>     the AI keys, e.g. sefardic/strict
       full | mini            the conversation size (overlay is implicit)
       calendar/<YYYY-MM-DD>  the calendar day
       signin | profile | settings   the account modal
```

For example `/chat/new/all/balanced`, `/answer/<uuid>/sefardic/strict`, `/text/Genesis.1/chat/<uuid>/ashkenaz/sources/mini`.

- **Views** (`VIEW_KEYS`: `text`, `prayer`, `community`, `siddur`, `chat`, `a`, `history`) are mutually exclusive. The **overlay** keys (`date`, `conversation` + `cv`, and the AI keys `minhag` and `mode`) ride on top of whichever view is underneath. **Display keys** (`lang`, `layout`, `vowels`, `taamim`, `translit`, `size`) are modifiers that survive view changes and `clearRoute`. A value outside the allowed set is dropped, never guessed at.
- The server mirrors the grammar in `backend/routes_spa_paths.py`, including the 308 redirects for trailing slashes and legacy query URLs. `docs/API.md` has the route table and the noindex rules.
- `chat`, `a` and `history` are private or per-reader views, so they are never indexed.
- `main.js` calls `installRouter(handler)` (popstate) and then hydrates the URL the page actually loaded with, since `popstate` never fires for it. Back/Forward restores the reader's scroll position per entry.

---

## 6. Theme, tokens and motion

### Theme

The theme is an attribute on `<html>`: `data-theme="light" | "dark"`, with `theme-dark` kept as a class on `<html>` and `<body>` as a migration alias for older selectors. The preference has three values (`light`, `dark`, `system`) and the topbar button cycles them. The inline bootstrap sets the attribute before first paint, and `tokens.css` also carries a `@media (prefers-color-scheme: dark)` block scoped to `:root:not([data-theme="light"])`, so a page with no script still paints in the system theme. Components never hard-code a colour: they reference a token, and the dark values live in one `[data-theme="dark"]` block in `tokens.css`.

### Token layer: `static/css/tokens.css`

The single source of truth, loaded before every other sheet.

| Group | Examples |
|---|---|
| Surfaces | `--surface-0` (page) to `--surface-3` (topbar), `--surface-pill`, `--surface-border`, `--surface-scrim` |
| Ink (text) | `--ink-primary`, `--ink-secondary`, `--ink-heading`, `--ink-on-accent` |
| Accents | `--accent-primary`, `--accent-gold`, `--accent-green`, `--accent-red`, … (gold is for icons and borders, never for body text on light) |
| Controls | `--ctrl-bg`, `--ctrl-border`, `--ctrl-text`, `--ctrl-filled-bg`, … |
| AI | `--ai-surface`, `--ai-border`, `--ai-src-*`, `--ai-warning-*` |
| Loading | `--load-skeleton-base`, `--load-skeleton-highlight`, `--load-skeleton-shimmer` |
| Calendar | `--cal-event-holiday`, `--cal-event-shabbat`, … (kept in step with the backend's Hebcal colour palette) |
| Geometry | `--radius-xs` to `--radius-full`, `--sp-1` to `--sp-12` (a 4 px / 8 px grid), `--shadow-sm/md/lg` |
| Type | `--text-xs` to `--text-3xl` (fluid `clamp()` sizes; these are **sizes**, not colours), `--font-serif` (Cardo), `--font-sans` (Inter), `--font-hebrew` (Ezra SIL) |
| Motion | `--motion-dur-*`, `--motion-ease-out/spring/decel`, `--motion-dur-view-*`, `--motion-view-shift` |
| Glass | `--glass-bg`, `--glass-edge`, `--glass-shadow` (the translucent surfaces) |

Legacy aliases (`--surface-base`, `--text-main`, `--text-muted`, `--ink`, `--paper`, `--control-*`, …) remain so untouched sheets keep working; new code uses the semantic names above.

Fonts: Ezra SIL is self-hosted (`static/fonts/SILEOT.woff2`, preloaded); Cardo and Inter come from Google Fonts. Size-matched local fallback faces (`Cardo Fallback`, `Ezra SIL Fallback`) keep text from reflowing when the web fonts swap in.

### Motion

Two layers, per `.agents/ENGINEERING_RULES.md`:

1. **CSS keyframes** live in `tokens.css` (`shelah-fade-in`, `-fade-up`, `-scale-in`, `-slide-in-left/right`, `-slide-down-fade`, `-overlay-in`, `-shimmer`, `-spin`, `-dot-bounce`, `-warm-pulse`, `-attention-ping`, `-cite-highlight`, `-view-in`, `-view-out`) plus the Ask panel's five (`conv-rise`, `conv-cite-flash`, `conv-citepop-in`, `conv-status-blink`, `conv-status-dot`, which kept their `conv-` names). Feature sheets and inline template styles apply them by name. The rule is no `@keyframes` anywhere but `tokens.css`, and `tests/test_css_keyframes_guard.py` fails the build if one appears elsewhere.
2. **`static/js/motion.js`** (`window.ShelahMotion`) wraps the vanilla motion.dev `animate()` for anything JavaScript mounts, unmounts or moves:

| Helper | Use for |
|---|---|
| `animateIn` / `animateOut` | Mount/unmount fade and slide |
| `staggerIn` | List and grid entrances |
| `springMove`, `springAnimate`, `springValue`, `appleSpring` / `APPLE_SPRING` | Movement with spring physics (velocity hand-off supported) |
| `fadeOpacity`, `crossFade` | Opacity-only tweens, swapping two elements |
| `createPresence` | Manual show/hide lifecycle |
| `slideIn` / `slideOut` / `slideTo` | Drawers and sidebars |
| `present` / `dismiss` | Popovers, cards and sheets that grow from, and fold back into, their trigger |
| `transitionView`, `viewTransitionsOn` | The View Transitions API path for view changes, off unless the `data-view-transitions` flag is on |

Spring for movement, tween only for opacity and colour, animate only `transform` and `opacity`. Every helper checks `isMotionReduced()` first and falls back to an instant state change.

**Reduced motion.** `typography.css` carries the global `prefers-reduced-motion: reduce` fallback (near-zero durations, one iteration, no smooth scroll). Feature sheets (`loading.css`, `ai.css`, …) additionally set `animation: none` so the end state is static, and every skeleton disables its shimmer.

---

## 7. Loading, failure and recovery

`load-region.js` is the one contract for every async region (audit L-8):

- **Busy:** `aria-busy="true"` on the region plus a separate `role="status"` line ("Loading…" / "Loaded"), because `aria-busy` alone announces nothing.
- **Failed:** a `role="alert"` message with a focusable Retry button, or none when retrying cannot help (a text that does not exist), plus any actions the caller adds ("Back to prayers", "Search for …").
- **Recovery:** automatic retries after 2 s and 6 s, then manual only; one more when the connection returns; aging data can revalidate when the tab becomes visible.
- **Cancellation:** one `AbortController` per region; a new load, or `abortRegion()`, aborts the old one and clears every timer and listener it left.

Skeletons match the final layout's shape so nothing shifts when data arrives. Offline, the service worker answers from cache and falls back to `/static/offline.html` for a navigation it cannot serve.

---

## 8. The siddur

The checked-in Edot HaMizrach siddur (`data/siddur/`, built by `scripts/build_siddur.py`, served by `/api/siddur/v2/*` in `backend/routes_siddur.py`). `main.js` builds `window.ShelahSiddur = createSiddur({ deps })`; the inline `displaySiddur()` calls its `prepare(value)` / `mount(container, prepared)` pair, so a route change that arrives mid-load drops the stale page before anything is drawn.

- **`siddur.js`**: `createSiddur({ fetchImpl, deps, storage, win, nav, doc })` loads the contents and each service (`?v=<data version>`, so a rebuild never serves a stale copy), draws the page, keeps the URL on the section being read (`replaceState`, so Back leaves the service), saves the reading position, and owns the screen wake lock. Also `renderToc(mount)` for the Texts menu and sidebar, and `SiddurNotFound`.
- **`siddur-reader.js`**: pure markup and helpers: typed lines (`prayer` / `instruction` / `conditional` / `heading`, inline rubrics as `<small>`), the contents, section tracking, saved positions, the wake lock, and `keepOffline(toc)`.
- **`siddur-day.js`**: the "Today in the siddur" card: which Hebrew day it is (after sunset it is tomorrow's), Israel or Diaspora (guessed from the zmanim location, then the reader's own choice), and per-service reminders built from `/api/siddur/v2/day` (Tachanun, Hallel, Mashiv/Morid, Barech Alenu, Ya'aleh VeYavo, Al HaNissim, the Ten Days, Aneinu, Musaf, the Omer). Reminders sit beside the text; nothing in the siddur is hidden or rewritten.

**Offline.** The first time a reader opens a siddur page, `keepOffline` posts `PRECACHE_SIDDUR` to the service worker, which keeps the contents and every service (about 600 KB gzipped) in a cache named for the siddur's data version (`shelah-siddur-<rite>@<version>`), not the deploy, so deploys do not discard it. A versioned service is then answered from that cache with no network at all, and the contents fall back to it offline. In the installed app only, it also asks `navigator.storage.persist()` so the browser will not evict it.

---

## 9. Zmanim

`zmanim.js` holds the zman-clock panel (`installZmanim(deps)`, called once from `main.js`) and, for historical reasons, the unrelated daily-study prefetch (`installDailyPrewarm()` / `prewarmDailyStudy()`). `installZmanim(deps)` takes an explicit `deps` object (`t`, `isHebrewMode`, `translateHolidayName`, `formatOmerLabel`, `formatWeeklyShabbatLabel`, `translateShabbatWarning`, `escapeHtml`, `setHebrewDate`); every other export takes the same object, so each can be called or tested without `installZmanim`.

- Location comes from a previously picked city (`localStorage["Sh'elahLastLocation"]`) or the session cookie set by `/set_location`, falling back server-side to IP-based geolocation. There is no `navigator.geolocation` permission prompt.
- It fetches `/api/zmanim` (with `lat`/`lon` for an explicit location), highlights the next upcoming zman and keeps a live countdown, rescheduling itself with `setTimeout` once a second (not a polling interval).
- A language switch re-renders through `refreshZmanimDisplay(deps)` without a re-fetch.

---

## 10. Service worker and PWA

`static/service-worker.js` is registered from `index.html` at `/service-worker.js?swv=3` (`updateViaCache: 'none'`), served from the site root so its scope covers the app. The manifest is `/manifest.webmanifest`; icons are in `static/`. Four caches, all named for `CACHE_VERSION` (bump it whenever the shell asset list changes): shell, runtime, API, and prewarm, plus the siddur's own cache.

| Request | Strategy |
|---|---|
| HTML navigation, scripts, time-sensitive reads (`/api/zmanim*`, `/api/daily-study`, `/api/parasha`, `/api/holidays`) | Network first, cached copy only when offline |
| Static assets and other API reads | Stale-while-revalidate |
| `/api/user/`, `/api/bookmarks/`, `/api/auth/`, `/api/client-errors`, `/api/conversations`, `/api/public/answer` | **Never cached** (per-user or revocable data) |
| Failed navigation with nothing cached | `/static/offline.html` |
| Daily-study refs (Daf Yomi, Rambam, parasha) | Prewarmed by `zmanim.js` through a message to the worker |

`tests_js/service_worker.test.js` covers the routing rules.

---

## 11. CSS architecture

- **Tokens first.** Colours, spacing, radii, type and motion are tokens in `tokens.css`. No raw hex in a component, no magic-number margins: spacing uses `--sp-*` on the 4 px / 8 px scale.
- **Feature sheets** under `static/css/`:

| File | Owns |
|---|---|
| `tokens.css` | Tokens, keyframes, fallback font faces |
| `typography.css` | Fluid type, Hebrew stack, RTL overrides, the reduced-motion fallback |
| `reader.css` | Text reader, bilingual columns, section navigation |
| `sidebar.css` | Sidebars and tab bar |
| `halacha.css`, `prayer.css` | Halacha sections and community badges; prayer layout |
| `siddur.css` | The siddur reader and its day card |
| `ai.css` | Source cards, AI surfaces |
| `conversation.css` | The Ask panel, turns, citations, mini/full/overlay layouts |
| `history.css` | The `/history` page and the shelf |
| `calendar.css`, `calendar-detail.css` | Calendar overlay; the detail card |
| `loading.css` | Skeletons and the load-region states |
| `legal.css` | Legal, about, help and glossary pages |
| `tailwind.css` | Built output (below) |

- **`static/style.css`** (about 5,000 lines) is the legacy remainder. Move a selector out by relocating it to the right feature sheet, deleting it from `style.css`, and checking light and dark before committing.
- **Tailwind** is built, not loaded from a CDN: `npm run build:css` compiles `static/css/tailwind.input.css` with `tailwind.config.js` (content: `templates/**/*.html`, `static/js/**/*.js`) into the committed, minified `tailwind.css`. Re-run it when you add a utility class that was not used before. DaisyUI's stylesheet loads from jsDelivr with an SRI hash.
- **Touch targets** are at least 44 by 44 px, set with `min-height`/`min-width` or padding, never left to the content size.
- **Accessibility** is checked in both themes (`npm run test:a11y` runs pa11y-ci for light and `scripts/a11y_dark_scan.js` for dark); see `docs/ACCESSIBILITY_AUDIT.md` for the standing contrast findings.

---

## 12. Adding a module

1. Create `static/js/my-feature.js` with real `import`/`export` (a file with neither is treated as a classic script and is not versioned by the import map).
2. Export an `installMyFeature(deps)` function that attaches listeners and subscribes to state. Take anything it needs from the inline script as an explicit `deps` argument.
3. Import and call it from `main.js`; if the inline script must call it, add it to the bridge in section 3 and remove the entry when the last caller is gone.
4. Add `tests_js/my_feature.test.js` (the suite is `node --test`; modules that touch `window` are tested through the harness in `tests_js/helpers/`).
5. If it is part of the app shell, add it to `CORE_ASSETS` in `static/service-worker.js` and bump `CACHE_VERSION`.
6. Add its stylesheet to the `index.html` link list after `tokens.css`, and wrap every async region in `ShelahLoadRegion`.
