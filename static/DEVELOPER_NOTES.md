# Static Assets Notes

## `style.css`

Main custom stylesheet layered on top of Tailwind utility classes from `templates/index.html`.

### Major sections

- Typography and Hebrew support:
  - Local Ezra SIL font-face and Hebrew direction/text defaults.
- Reader personalization:
  - CSS variables (`--reader-font-size`, `--reader-line-height`).
  - Hebrew size offset relative to English for visual parity.
- Reader row layouts:
  - Side-by-side/interleaved/single row spacing and label placement.
- Sidebar responsive behavior:
  - Desktop expansion when left/right panels are hidden.
- Mobile drawer behavior:
  - `mobile-drawer`, `mobile-open`, and `mobile-panel-open` classes under max-width media queries.

## `service-worker.js`

Offline-first caching behavior:
- Precaches minimal core shell assets.
- Cache-first for GET requests.
- Falls back to `offline.html` for failed navigation requests.

## `offline.html`

Simple offline fallback screen shown when app navigation cannot reach network.
