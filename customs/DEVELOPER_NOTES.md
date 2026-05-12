# Customs Data Notes

> Sync status (2026-05-12): No changes to customs data files this cycle. Sync status current.

This folder contains community minhag data files used by `/api/community/*` and customs lookups.

## File patterns

- Community-specific files (`ashkenaz.json`, `sefardic.json`, etc.):
  - Typically include identity metadata, `halacha_index`, and source registry data.
- Aggregated file (`customs_db.json`):
  - Legacy/alternate shape supported by loader logic in `customs.py`.

## How runtime uses this data

1. `customs.py` loads all JSON files.
2. It normalizes different schema shapes into a searchable in-memory map.
3. Fuzzy matching/keyword matching returns relevant custom rulings and notes.

## Editing guidance

- Preserve valid JSON and UTF-8 encoding.
- Keep topic/category fields consistent to improve matcher quality.
- Include source metadata where possible for transparency in UI output.
