# Templates Notes

## `index.html` responsibilities

- Main single-page shell for the entire UI.
- Includes Tailwind config, top nav, sidebars, reader area, settings panel, and modal/dialog markup.
- Contains the central frontend controller script (state, rendering, API calls, interactions).

## Key UI zones

- Top bar: search, language toggle, settings shortcuts.
- Left sidebar (`#leftSidebar`): text navigation and library tree.
- Center content (`#mainContainer`): home cards and reader.
- Right sidebar (`#rightSidebar`): zmanim/today panel and daily learning cards.

## Reader system

- Layout modes: bilingual, bilingual-reverse, interleaved, hebrew-only, english-only.
- Reader preferences are stored in `appState.prefs` and applied by `applyReaderPreferences()`.
- Hebrew text controls include toggles for vowels and cantillation.

## Mobile behavior

- Mobile controls: `#mobileNavBtn`, `#mobilePulseBtn`, `#mobileDrawerBackdrop`.
- Drawer state is managed by `openMobilePanel`, `closeMobilePanels`, and `toggleMobilePanel`.
- Mobile breakpoint logic uses `isMobileViewport()` and CSS media queries in `static/style.css`.

## API integration points

Frontend calls backend endpoints for:
- Library/text data (`/api/library/*`, `/api/text/*`).
- Prayer and community content (`/api/prayer/*`, `/api/community/*`).
- Time/calendar widgets (`/api/zmanim*`, `/api/holidays`, `/api/parasha`).
