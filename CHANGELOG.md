# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
