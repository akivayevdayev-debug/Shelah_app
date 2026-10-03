# Templates notes

## `index.html`: the application shell

The single-page app shell, about 14,500 lines. It holds the markup for the whole UI, the no-flash theme and preference bootstraps in `<head>`, the import map for the ES modules under `static/js/`, and one classic inline script that owns most of the interactive behaviour (state, rendering, the reader, the calendar, the settings panel). Newer surfaces live in ES modules and reach the inline script through the `window.Shelah*` bridges. Details: [`docs/FRONTEND.md`](../docs/FRONTEND.md).

Tailwind is **built** (`static/css/tailwind.css`, via `npm run build:css`), not configured inline. After adding utility classes here, rebuild it.

### Page zones

| Zone | Element | Holds |
|---|---|---|
| Top bar | `<header role="banner">` | Search, language toggle, the Ask entry, account and settings controls |
| Left sidebar | `#leftSidebar` | Text tree, siddur contents, community list |
| Main content | `#mainContainer` | `#homeGrid` (home cards), the reader, prayer, community and history pages |
| Commentary panel | `#rightSidebar` | Commentary on the selected passage, or the verse in view in reading mode |
| Ask Sh'elah | `#convPanel` (plus `#convPip`, `#convHistoryPop`, `#mobileSearchTray`) | Multi-turn AI conversations and search answers, owned by `conversation-ui.js` |

### Reader

Layout modes are bilingual, bilingual-reverse, interleaved, Hebrew-only and English-only. Preferences live in `prefs` (persisted under `localStorage["Sh'elahPrefs"]`) and are applied by `applyReaderPreferences()`. Hebrew vowel and cantillation toggles are part of the same preferences.

### Mobile

On narrow viewports the sidebars become drawers (`#mobileDrawerBackdrop`, `openMobilePanel`, `closeMobilePanels`, `toggleMobilePanel`, `isMobileViewport`), and a bottom tab bar (`#mobileBottomTabs`) plus a search tray (`#mobileSearchTray`, with the Ask and History buttons) replace the desktop chrome. Breakpoint rules live in `static/style.css` and the per-surface sheets.

### Backend contract

The shell is rendered by Flask with per-URL `<head>` values from `backend/page_meta.py`; path deep links (`/library/...`, `/siddur/...`, `/conversations/...`, shared answers) are served by `backend/routes_spa_paths.py` and parsed in the browser by `static/js/router.js`. Both ends must agree on the path grammar. API usage by surface is listed in [`docs/API.md`](../docs/API.md).

## Other templates

| Template | Route | Owner |
|---|---|---|
| `about.html`, `help.html`, `glossary.html` | `/about`, `/help`, `/glossary` | `backend/routes_pages.py` |
| `terms.html`, `privacy.html`, `accessibility.html` | `/terms`, `/privacy`, `/accessibility` | `app.py` |
| `ai-disclosure.html`, `acceptable-use.html`, `dmca.html`, `licenses.html` | same-named routes | `backend/routes_legal.py` |
| `404.html` | the not-found handler | `app.py` |
| `components/` | `theme_bootstrap.html` (no-flash theme script), `icons.html`, and the shared `legal_topbar`, `legal_footer`, `legal_scripts`, `site_footer` partials | included by the pages above |

The legal and information pages are server-rendered and do not load the SPA. Their content is a legal matter; do not change wording without the owner's say-so.
