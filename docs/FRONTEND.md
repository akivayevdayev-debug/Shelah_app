# Frontend Architecture

Sh'elah's frontend is a single-page application rendered from `templates/index.html` (11 200 lines). JavaScript is being migrated progressively from inline `<script>` blocks to ES modules under `static/js/`. CSS is being migrated from the legacy `static/style.css` monolith to per-feature sheets under `static/css/`.

---

## Module Map: `static/js/`

### `state.js` — pub/sub store

Owns the canonical client-side application state. All modules read and write state through this file; no module holds its own copy of shared data.

```js
// Reading state
import { getState } from './state.js';
const { currentView, prefs } = getState();

// Writing state (triggers subscribed listeners)
import { setState } from './state.js';
setState({ currentView: 'reader' });

// Subscribing to state changes
import { subscribe } from './state.js';
subscribe((newState, prevState) => {
  if (newState.currentView !== prevState.currentView) {
    // handle view change
  }
});
```

**Global state shape** (`appState`):

| Key | Type | Description |
|---|---|---|
| `currentView` | `string` | Active panel: `'home'`, `'reader'`, `'calendar'`, `'prayers'`, `'ai'` |
| `prefs` | `object` | User preferences: `{ theme, fontSize, community, language }` |
| `lastAiQuestion` | `string` | Most recent question submitted to the AI |
| `lastAiResponse` | `object` | Most recent parsed AI response payload |
| `suggestionItems` | `array` | Current list of suggestion chips displayed in the AI panel |
| `user` | `object \| null` | Clerk user object, or `null` if unauthenticated |
| `calendarData` | `object \| null` | Today's calendar payload from `/api/calendar/today` |
| `zmanimData` | `object \| null` | Zmanim for the user's current location |

---

### `ai-service.js` — AI request handler

Exports `askAi(question, options)` — the single function responsible for submitting questions to `POST /ask` and updating state with the response.

```js
import { askAi } from './ai-service.js';

const response = await askAi('Can I use electricity on Shabbat?', {
  community: 'ashkenaz',
  mode: 'balanced',
  language: 'en',
});
// response: { answer, sources, customs, wiki, meta, confidence }
```

Internally, `askAi` reads the Clerk token from `window.Clerk.session`, builds the fetch request, handles 429 (rate limit) and 503 (service unavailable) gracefully, and calls `setState({ lastAiQuestion, lastAiResponse })` on success.

---

### `reader-ui.js` — text reader panel

Controls the Sefaria text reader panel. Exports `installReader()` called from `main.js`.

Key behaviours:

- Renders bilingual (Hebrew + English) text in a side-by-side or stacked layout depending on viewport width
- Handles navigation between sections (prev/next chapter)
- Syncs scroll position between Hebrew and English columns
- Stores the current ref in state as `currentView: 'reader'` + `readerRef`

---

### `zmanim.js` — calendar and zmanim UI

Controls the calendar panel. Exports `installZmanim()`.

Key behaviours:

- Requests the user's geolocation via `navigator.geolocation.getCurrentPosition`; falls back to IP-based location
- Fetches `/api/calendar/today?lat=…&lon=…` and renders the daily calendar card
- Renders the zmanim times list with colour-coded proximity indicators (next zman highlighted)
- Polls for zmanim updates every 60 seconds while the calendar panel is active

---

### `main.js` — bootstrap

The entry point. Imported by `templates/index.html` as a module script. Responsibilities:

1. Imports and calls all `install*()` functions from the other modules
2. Initialises Clerk (`window.Clerk`) and passes the user object to state
3. Loads saved preferences from `localStorage` and merges with Supabase preferences (if authenticated)
4. Sets up the theme from `prefs.theme`
5. Registers the service worker (`/static/service-worker.js`)

```js
// main.js pattern
import { installReader } from './reader-ui.js';
import { installZmanim } from './zmanim.js';
// ... other modules

installReader();
installZmanim();
// ...
```

---

## `window.ShelahModules` Bridge

Legacy inline script blocks in `templates/index.html` that have not yet been migrated to ES modules use the `window.ShelahModules` bridge object to call into module code:

```html
<script type="module">
  import { askAi } from '/static/js/ai-service.js';
  import { setState } from '/static/js/state.js';
  window.ShelahModules = { askAi, setState };
</script>

<script>
  // Legacy inline code can now call:
  window.ShelahModules.askAi('What is the ruling on…');
</script>
```

When migrating a legacy block to a module, remove its entry from `window.ShelahModules` once the inline reference is gone.

---

## Theme System

The active theme is controlled by a class on `<body>`:

```js
// Apply dark theme
document.body.classList.add('theme-dark');

// Apply light theme
document.body.classList.remove('theme-dark');
```

All colours are CSS custom properties defined on `:root` (light mode defaults) and overridden under `.theme-dark`. Never hard-code colour values in component styles — always reference a CSS variable.

### Custom property namespaces

| Prefix | Purpose |
|---|---|
| `--surface-*` | Background surfaces: `--surface-bg`, `--surface-card`, `--surface-overlay` |
| `--text-*` | Text colours: `--text-primary`, `--text-secondary`, `--text-muted`, `--text-inverse` |
| `--accent-*` | Brand accent colours: `--accent-primary`, `--accent-secondary`, `--accent-highlight` |
| `--ai-*` | AI panel specific: `--ai-bubble-bg`, `--ai-source-border`, `--ai-confidence-bar` |
| `--border-*` | Border colours: `--border-default`, `--border-focus`, `--border-subtle` |
| `--spacing-*` | Spacing scale (4 px base): `--spacing-1` through `--spacing-12` |
| `--radius-*` | Border radius: `--radius-sm`, `--radius-md`, `--radius-lg` |

---

## Motion Conventions

Animation classes are applied with JavaScript by adding/removing class names. All transitions use CSS custom properties for duration and easing, and must honour `prefers-reduced-motion`.

### Animation classes

| Class | Effect | Duration |
|---|---|---|
| `animate-fade-up` | Element fades in and translates upward | 200 ms |
| `animate-fade-in` | Element fades in from opacity 0 | 150 ms |
| `animate-scale-in` | Element scales from 0.95 to 1 while fading in | 200 ms |

### Reduced-motion guard

```css
@media (prefers-reduced-motion: reduce) {
  .animate-fade-up,
  .animate-fade-in,
  .animate-scale-in {
    animation: none;
    transition: none;
  }
}
```

All skeleton loading states (shown while async data loads) also disable their shimmer animation under `prefers-reduced-motion`.

---

## Adding a New ES Module

1. Create the file at `static/js/my-feature.js`.

2. Export an `installMyFeature()` function that sets up event listeners, fetches initial data, and subscribes to relevant state keys:

```js
// static/js/my-feature.js
import { getState, setState, subscribe } from './state.js';

export function installMyFeature() {
  // Set up DOM listeners
  document.getElementById('my-button')?.addEventListener('click', handleClick);

  // Subscribe to state changes
  subscribe((newState) => {
    if (newState.currentView === 'my-feature') {
      render();
    }
  });
}

function handleClick() {
  setState({ currentView: 'my-feature' });
}

function render() {
  // Update the DOM
}
```

3. Import and call `installMyFeature()` from `main.js`:

```js
// static/js/main.js
import { installMyFeature } from './my-feature.js';
installMyFeature();
```

4. If legacy inline code needs to call into your module, export the relevant function and add it to `window.ShelahModules` in the module bootstrap block in `index.html`.

---

## CSS Architecture

### Token layer

All design tokens live as CSS custom properties on `:root` in `static/style.css` (the legacy file) and are being progressively moved to a dedicated `static/css/tokens.css`. Do not define colour, spacing, or radius values anywhere other than the token layer.

### Feature stylesheets

Each feature has its own file under `static/css/`:

| File | Owns |
|---|---|
| `ai.css` | AI panel, source cards, confidence bar, suggestion chips |
| `calendar.css` | Calendar panel, zmanim list, holiday badges |
| `halacha.css` | Halacha answer sections, ruling display, community badges |
| `prayer.css` | Prayer service layout, bilingual prayer grid, rubric text |
| `reader.css` | Text reader panel, bilingual columns, section navigation |
| `sidebar.css` | Sidebar navigation, tab bar, community selector |
| `typography.css` | Fluid type scale (`clamp()`-based), Hebrew font stack, RTL overrides |

### Migrating a selector out of `style.css`

1. Identify the selector in `static/style.css`.
2. Move it to the appropriate feature stylesheet under `static/css/`.
3. Ensure the feature stylesheet is imported in `index.html` (it should already be if the feature exists).
4. Delete the selector from `static/style.css`.
5. Test in both light and dark mode before committing.

### Spacing scale

Use the 4 px base spacing scale via CSS custom properties. Never use magic-number `margin` or `padding` values:

```css
/* Correct */
padding: var(--spacing-4);   /* 16px */
gap: var(--spacing-2);       /* 8px */

/* Wrong */
padding: 16px;
gap: 8px;
```

### Touch targets

All interactive elements must meet the 44×44 px minimum touch target size. Use `min-height` and `min-width` or padding to achieve this — do not rely on the content size alone.
