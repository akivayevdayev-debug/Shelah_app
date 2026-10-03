# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Everything since 1.0.0 (2026-09-16). Nothing has been tagged since, so this section covers about 240 commits up to 2026-10-03.

### Added
- **Ask Sh'elah conversations.** Multi-turn AI conversations stored per user (Supabase `conversations`, `messages`, `citations`), shown in an in-page panel that cross-fades rather than opening a modal, with a history popover and, on phones, a search tray. Search answers open in the same panel. The Mode and Community selects moved into the panel.
- **Shareable answers.** `/ask` returns a history id, and a stored answer can be opened at `/answer/<id>` through a public share link.
- **Path deep links.** Everything the app writes into the URL moved from query parameters to path segments (`/text/Genesis.1`, `/library/...`, `/siddur/...`, `/conversations/...`), with redirects for the legacy `?text=`, `?prayer=` and `?community=` links and a per-URL `<head>` (title, canonical tag, sharing metadata) instead of one head for every path.
- **Rebuilt prayers and siddur.** A checked-in Edot HaMizrach siddur built from Sefaria-Export (CC0 and Public Domain versions only), a `/siddur` reader with a sticky section picker, and a Today view driven by zmanim and the Hebrew date: when to say Tachanun, full or half Hallel, Mashiv HaRuach and similar. The siddur caches for offline use once opened, and the AI's prayer lookup checks the curated siddur before Sefaria search.
- **AI answer presentation.** Numbered source chips (`[1]`, `[2]`, ...) with working links, live progress while an answer is produced (an NDJSON stream), a depth and minhag prompt revision, and Hebrew-language sources, source labels and community names around answers in Hebrew mode.
- **Calendar.** A holiday detail card (a popover on desktop and a drag-to-dismiss bottom sheet on phones), a day-agenda view, clickable "Today" and "This Week's Shabbat" entries on the home page, and fast-day start and end times in the zmanim.
- **Reader.** Commentary for the verse being read is preloaded, and scrolling up loads the previous section without moving the place you were reading.
- **Hebrew localisation** of the calendar, zmanim panels, location search, parasha fallback and several commentator names.
- **Retrieval guard.** A phrase screen for retrieved third-party text before it reaches the model, with an opt-in live red-team harness (`scripts/redteam_retrieved_context.py`).
- **Operations documentation.** Runbooks (deploy, rollback, incident response, spend guardrails), a cost-optimisation guide, an environment-variable reference, a database reference with a script that regenerates it from the live schema, an AI security review, and a standing whole-site audit prompt.

### Changed
- **Runtime and dependencies.** Python 3.14; every dependency installs from hash-locked files in CI; the CI runner is pinned to `ubuntu-24.04`; `pip-audit` findings in both lock files were cleared.
- **Cost and delivery.** The cost meter was repriced and spend is now attributed to the user on the synchronous `/ask` path too; retries and timeouts are clamped to the remaining request budget.
- **Interface.** Windows present with a soft fade and scale; the mobile top and bottom bars are part of the page layout rather than floating overlays; the library focus ring and several dark-theme contrasts were corrected to meet WCAG 2.1 AA; the Privacy and Data window is a native `<dialog>`; repeated markup (theme bootstrap, legal footer) moved into shared partials.
- **Design tokens.** Calendar colours, motion durations and easing, and other per-file duplicates were consolidated into `static/css/tokens.css`; every `@keyframes` rule now lives there (the Ask panel's five moved over, and the privacy dialog's spinner reuses `shelah-spin`), and a test fails if one is defined anywhere else.
- **Tests and quality gates.** The coverage gate rose from 60% to 85% against `backend/`; the JavaScript suite grew to cover most modules and the CI helper scripts; SonarCloud's findings (complexity, regexes, duplicated code, test smells) were worked through. The suite is now about 4,700 Python and 600 Node tests.
- **Documentation.** The README, `docs/API.md`, `docs/FRONTEND.md`, `docs/SERVICE_ARCHITECTURE.md`, `docs/SECURITY.md`, the developer notes, the launch checklist and the decision log were rewritten against the current code, and references to internal planning documents were removed from documentation, code comments and configuration.

### Fixed
- **Row Level Security for Clerk users.** `auth.uid()` was casting the token subject to a UUID, so RLS silently failed for every Clerk-authenticated request; the user-id columns became text and the policies now compare the token subject as text. Postgres rejections of expired or slightly skewed tokens now log quietly.
- **Legal consent** acceptance had never been persisting, because the upsert keyed on a column that did not exist.
- **Privacy operations.** The data export and account deletion sweep now include feedback rows, and those routes, with the Clerk webhook, have their own rate-limit classes.
- **Error responses** on the community endpoints no longer return raw exception text to the client.
- **Availability.** A Redis outage no longer makes every request, static assets included, wait out the connect timeout; the community-knowledge query no longer retries a table that is known to be down on every question; a Gemini outage can no longer hold a synchronous worker for minutes.
- **Calendar and zmanim.** "Today" in the monthly view uses the requested location's timezone; the holiday chips, agenda and tooltips follow the display language; failed fetches now show an error instead of leaving rows stuck.
- **Reader and siddur.** Inline bold and italic markup is kept in reader and siddur text; tapping a word for its meaning no longer hangs while it tries several spelling variants; the commentary sidebar shows a loading state instead of nothing; Shul mode's top bar spans the text column.
- **Accessibility.** Icon-only buttons gained accessible names, including the two whose name was only assigned by script after they appeared.
- **Library.** The April crawl's removals were almost entirely false positives (complex-schema works probed with bare titles); they are reinstated, and the crawler's key normalisation no longer drops Hebrew letters.
- **Verification scripts.** The integration, feedback-migration and RLS checks no longer pass on a wrong or empty response.
- **Test determinism.** Sefaria tests no longer read leftover disk caches, which had made coverage look better than it was.

### Security
- The leaked Gemini key (rotated 2026-09-02) was scrubbed from the repository history; what remains exposed is described in `docs/SECURITY.md`.
- Path-traversal and super-linear regex findings were fixed in the documentation-sync checker, the text engine, the citation parser and the Hebcal reading parser.
- Backend Sentry throttling mirrors the frontend's.
- The npm audit's high findings (one dependency chain through `pa11y`) were cleared, and the `pa11y-ci` `undici` and `brace-expansion` advisories followed with in-range lockfile updates. The five that remain are all in the Tailwind 3 build chain (dev tooling only).

## [1.0.0] - 2026-09-16

Initial stable release of Sh'elah, a full-stack web application for Jewish text study, halachic inquiry, and daily practice: it integrates the Sefaria text library, community customs datasets, prayer resources, zmanim, and a multi-model AI layer to answer halachic questions with source citations in the user's community tradition.

### Added
- AI-generated halachic Q&A with source citations (Google Gemini primary, Anthropic Claude fallback), aware of multiple community traditions (Sefardic, Ashkenaz, Yemenite, and others).
- Bilingual (EN/HE) Sefaria text reader with RTL layout support.
- Zmanim and calendar tools, including FullCalendar-based scheduling.
- Community customs and prayer resources.
- User accounts and authentication via Clerk, with Supabase-backed storage for user data such as bookmarks and preferences.
- Accessibility CI gate (pa11y-ci, WCAG 2.1 AA) and an explicit "AI-generated" disclosure banner on AI answers.

### Changed
- Ongoing reader, calendar, and AI-response UI refinements accumulated over the project's development history (source-box states, event coloring, scroll behavior, and related polish).

### Fixed
- Numerous bug fixes across the reader, calendar, AI pipeline, and test/CI infrastructure, including HTML/URI sanitization hardening for AI- and prayer-sourced content and stability fixes to the automated test suite.
